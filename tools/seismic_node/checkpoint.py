"""Transactional checkpoint installation, rollback, and backup deletion.

The installer treats Reth and Summit state as one atomic unit. Before replacing
anything it validates paths and artifacts, acquires Summit's process lock, moves
all mutable state into a receipt-backed rollback directory, and leaves every
service stopped. Recovery relies on same-filesystem ``rename(2)`` operations;
copying live databases is intentionally avoided.
"""

from __future__ import annotations

import argparse
import datetime as dt
import errno
import fcntl
import hashlib
import json
import os
import re
import shlex
import shutil
import stat
import sys
import tarfile
import tempfile
import tomllib
from pathlib import Path, PurePosixPath
from typing import Any

from . import supervisor as supervisor_control

INVENTORY_VERSION = 1
MANIFEST_VERSION = 1
WEAK_SUBJECTIVITY_MAX_AGE_EPOCHS = 5
DEFAULT_INSTALLED_WEAK_SUBJECTIVITY_PATH = Path("/etc/seismic/weak-subjectivity.toml")
CHECKPOINT_CONFIG_PATHS = {
    "validator": Path("/etc/seismic/validator-checkpoint-start.toml"),
    "observer": Path("/etc/seismic/observer-checkpoint-start.toml"),
}
DEFAULT_INVENTORY_PATHS = {
    "validator": Path("/etc/seismic/validator-installation.toml"),
    "observer": Path("/etc/seismic/observer-installation.toml"),
}
SERVICE_PROGRAMS = {
    "validator": (
        "custodian",
        "reth",
        "summit",
        "summit-checkpoint",
        "summit-deposit-rpc",
        "checkpointer",
    ),
    "observer": (
        "custodian",
        "reth",
        "summit-observer",
        "summit-observer-checkpoint",
        "checkpointer",
    ),
}
COMMON_INVENTORY_KEYS = {
    "schema_version",
    "reth_data_dir",
    "reth_p2p_key_path",
    "summit_data_dir",
    "summit_keys_dir",
}
OBSERVER_INVENTORY_KEYS = {
    "observer_parent_node_public_key",
    "observer_index",
}
HEX_DIGEST = re.compile(r"^0x[0-9a-fA-F]{64}$")
PUBLIC_KEY = re.compile(r"^[0-9a-f]{64}$")
LOCK_FILE_NAME = ".summit-process.lock"
RECEIPT_FILE_NAME = "rollback-manifest.json"


class CheckpointError(Exception):
    """An operator-actionable checkpoint installation error."""


# ---------------------------------------------------------------------------
# Path, ownership, and input-file validation
# ---------------------------------------------------------------------------


def require_root() -> None:
    """Require root because node state and receipts are root-managed."""
    if os.geteuid() != 0:
        raise CheckpointError("This command must be run as root (use sudo).")


def require_absolute(path: Path, description: str) -> None:
    """Reject relative, root, non-normalized, and symlink-traversing paths."""
    if not path.is_absolute() or path == Path("/"):
        raise CheckpointError(
            f"{description} must be an absolute non-root path: {path}"
        )
    if path.resolve(strict=False) != path:
        raise CheckpointError(
            f"{description} must be normalized and must not traverse symbolic links: {path}"
        )


def lstat_path(path: Path, description: str) -> os.stat_result:
    """Inspect a path itself rather than following a final symlink."""
    require_absolute(path, description)
    try:
        return path.lstat()
    except FileNotFoundError as error:
        raise CheckpointError(f"{description} is missing: {path}") from error
    except OSError as error:
        raise CheckpointError(
            f"Could not inspect {description}: {path}: {error}"
        ) from error


def require_directory(path: Path, description: str) -> os.stat_result:
    """Require an existing, non-symlinked directory."""
    metadata = lstat_path(path, description)
    if stat.S_ISLNK(metadata.st_mode) or not stat.S_ISDIR(metadata.st_mode):
        raise CheckpointError(f"{description} is not a safe directory: {path}")
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
        raise CheckpointError(f"{description} is not a safe regular file: {path}")
    if nonempty and metadata.st_size == 0:
        raise CheckpointError(f"{description} is empty: {path}")
    return metadata


def require_secure_root_parent(path: Path, description: str) -> None:
    """Verify every existing parent is a non-writable root-owned directory."""
    current = path.parent
    while True:
        try:
            metadata = current.lstat()
        except OSError as error:
            raise CheckpointError(
                f"Could not inspect {description} parent {current}: {error}"
            ) from error
        if stat.S_ISLNK(metadata.st_mode) or not stat.S_ISDIR(metadata.st_mode):
            raise CheckpointError(
                f"{description} parent is not a safe directory: {current}"
            )
        if metadata.st_uid != 0 or metadata.st_mode & 0o022:
            raise CheckpointError(
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
        raise CheckpointError(
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
                raise CheckpointError(
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
                raise CheckpointError(
                    f"Could not find an existing filesystem ancestor for {description}: {path}"
                )
            current = current.parent
            continue
        except OSError as error:
            raise CheckpointError(
                f"Could not inspect filesystem for {description}: {path}: {error}"
            ) from error
        if stat.S_ISLNK(metadata.st_mode):
            raise CheckpointError(
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
    raise CheckpointError(
        "Reth, Summit, checkpoint, and rollback paths must share a filesystem so "
        f"state moves are atomic ({details})"
    )


# Configuration files are parsed with exact schemas below. Unknown fields are
# rejected so misspelled safety settings cannot be silently ignored.


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
        raise CheckpointError(
            f"Could not safely open {description}: {path}: {error}"
        ) from error

    try:
        with os.fdopen(file_descriptor, "rb") as file:
            value = tomllib.load(file)
    except tomllib.TOMLDecodeError as error:
        raise CheckpointError(
            f"{description} is not valid TOML: {path}: {error}"
        ) from error
    if not isinstance(value, dict):
        raise CheckpointError(f"{description} must contain a TOML table: {path}")
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
    raise CheckpointError(f"{description} has an invalid schema ({'; '.join(details)})")


def inventory_path_value(inventory: dict[str, Any], key: str) -> Path:
    """Convert one inventory string into a validated absolute path."""
    value = inventory[key]
    if not isinstance(value, str) or not value:
        raise CheckpointError(
            f"Installation inventory {key} must be a non-empty string"
        )
    path = Path(value)
    require_absolute(path, f"Installation inventory {key}")
    return path


def load_inventory(role: str, path: Path) -> dict[str, Any]:
    """Load the installer's minimal inventory and verify on-disk identity."""
    inventory = read_toml(path, "Installation inventory", root_managed=True)
    expected = COMMON_INVENTORY_KEYS | (
        OBSERVER_INVENTORY_KEYS if role == "observer" else set()
    )
    require_exact_keys(inventory, expected, "Installation inventory")
    if inventory["schema_version"] != INVENTORY_VERSION:
        raise CheckpointError(
            f"Unsupported installation inventory schema_version: "
            f"{inventory['schema_version']!r}"
        )

    for key in (
        "reth_data_dir",
        "reth_p2p_key_path",
        "summit_data_dir",
        "summit_keys_dir",
    ):
        inventory[key] = inventory_path_value(inventory, key)

    require_separate_paths(
        {
            "Reth data directory": inventory["reth_data_dir"],
            "Reth P2P key": inventory["reth_p2p_key_path"],
            "Summit data directory": inventory["summit_data_dir"],
            "Summit keys directory": inventory["summit_keys_dir"],
        }
    )

    if role == "observer":
        parent_key = inventory["observer_parent_node_public_key"]
        if not isinstance(parent_key, str):
            raise CheckpointError("Observer parent node public key must be a string")
        parent_key = parent_key.removeprefix("0x").lower()
        if not PUBLIC_KEY.fullmatch(parent_key):
            raise CheckpointError("Observer parent node public key must be 32-byte hex")
        observer_index = inventory["observer_index"]
        if isinstance(observer_index, bool) or not isinstance(observer_index, int):
            raise CheckpointError("Observer index must be an integer")
        if not 0 <= observer_index <= 255:
            raise CheckpointError("Observer index must be in the range 0-255")
        inventory["observer_parent_node_public_key"] = parent_key

    validate_installed_identity(role, inventory)
    return inventory


def validate_installed_identity(role: str, inventory: dict[str, Any]) -> None:
    """Confirm persistent keys and observer assignment match the inventory."""
    require_directory(inventory["reth_data_dir"], "Reth data directory")
    require_regular_file(inventory["reth_p2p_key_path"], "Reth P2P key")
    require_directory(inventory["summit_data_dir"], "Summit data directory")
    summit_keys = inventory["summit_keys_dir"]
    require_directory(summit_keys, "Summit keys directory")
    require_regular_file(summit_keys / "node_key.pem", "Summit node key")
    require_regular_file(summit_keys / "consensus_key.pem", "Summit consensus key")

    if role == "observer":
        assignment_path = summit_keys / "observer-assignment"
        require_regular_file(assignment_path, "Observer assignment")
        try:
            assignment = assignment_path.read_text(encoding="utf-8").strip()
        except OSError as error:
            raise CheckpointError(
                f"Could not read observer assignment: {assignment_path}: {error}"
            ) from error
        expected = (
            f"{inventory['observer_parent_node_public_key']}:"
            f"{inventory['observer_index']}"
        )
        if assignment != expected:
            raise CheckpointError(
                f"Observer assignment mismatch: expected {expected}, found {assignment!r}"
            )


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
        raise CheckpointError(
            f"Could not safely open {description}: {path}: {error}"
        ) from error
    try:
        with os.fdopen(file_descriptor, "rb") as file:
            value = json.load(file)
    except (OSError, json.JSONDecodeError) as error:
        raise CheckpointError(
            f"Could not read {description}: {path}: {error}"
        ) from error
    if not isinstance(value, dict):
        raise CheckpointError(f"{description} must contain a JSON object: {path}")
    return value


def require_int(value: Any, description: str) -> int:
    """Require a non-negative JSON/TOML integer, excluding booleans."""
    if isinstance(value, bool) or not isinstance(value, int) or value < 0:
        raise CheckpointError(f"{description} must be a non-negative integer")
    return value


def require_digest(value: Any, description: str) -> str:
    """Require and normalize a 32-byte, 0x-prefixed hexadecimal digest."""
    if not isinstance(value, str) or not HEX_DIGEST.fullmatch(value):
        raise CheckpointError(f"{description} must be a 32-byte 0x-prefixed hex digest")
    return value.lower()


# ---------------------------------------------------------------------------
# Snapshot, archive, and weak-subjectivity validation
# ---------------------------------------------------------------------------


def load_manifest(path: Path) -> dict[str, Any]:
    """Parse a provider manifest with an exact versioned schema."""
    manifest = read_json(path, "Snapshot manifest", root_managed=True)
    require_exact_keys(
        manifest,
        {
            "version",
            "epoch",
            "summit_checkpoint_digest",
            "execution",
            "archive",
            "created_at",
        },
        "Snapshot manifest",
    )
    if manifest["version"] != MANIFEST_VERSION:
        raise CheckpointError(
            f"Unsupported snapshot manifest version: {manifest['version']!r}"
        )
    manifest["epoch"] = require_int(manifest["epoch"], "Snapshot manifest epoch")
    manifest["summit_checkpoint_digest"] = require_digest(
        manifest["summit_checkpoint_digest"], "Summit checkpoint digest"
    )

    execution = manifest["execution"]
    if not isinstance(execution, dict):
        raise CheckpointError("Snapshot manifest execution must be an object")
    require_exact_keys(
        execution, {"block_number", "block_hash", "state_root"}, "Execution identity"
    )
    execution["block_number"] = require_int(
        execution["block_number"], "Execution block number"
    )
    execution["block_hash"] = require_digest(
        execution["block_hash"], "Execution block hash"
    )
    execution["state_root"] = require_digest(
        execution["state_root"], "Execution state root"
    )

    archive = manifest["archive"]
    if not isinstance(archive, dict):
        raise CheckpointError("Snapshot manifest archive must be an object")
    require_exact_keys(archive, {"sha256", "size_bytes"}, "Archive identity")
    archive["sha256"] = require_digest(archive["sha256"], "Archive SHA-256")
    archive["size_bytes"] = require_int(archive["size_bytes"], "Archive size")
    if not isinstance(manifest["created_at"], str) or not manifest["created_at"]:
        raise CheckpointError("Snapshot manifest created_at must be a non-empty string")
    return manifest


def hash_file(path: Path) -> str:
    """Calculate a file's SHA-256 in bounded-memory chunks."""
    digest = hashlib.sha256()
    try:
        with path.open("rb") as file:
            while chunk := file.read(1024 * 1024):
                digest.update(chunk)
    except OSError as error:
        raise CheckpointError(
            f"Could not hash snapshot archive: {path}: {error}"
        ) from error
    return f"0x{digest.hexdigest()}"


def verify_archive_identity(archive_path: Path, manifest: dict[str, Any]) -> None:
    """Bind the local archive to the manifest's declared size and SHA-256."""
    metadata = require_root_managed_file(archive_path, "Snapshot archive")
    expected_size = manifest["archive"]["size_bytes"]
    if metadata.st_size != expected_size:
        raise CheckpointError(
            f"Snapshot archive size mismatch: expected {expected_size}, found {metadata.st_size}"
        )
    actual_hash = hash_file(archive_path)
    if actual_hash != manifest["archive"]["sha256"]:
        raise CheckpointError(
            f"Snapshot archive SHA-256 mismatch: expected "
            f"{manifest['archive']['sha256']}, found {actual_hash}"
        )


def validate_tar_members(archive: tarfile.TarFile) -> int:
    """Reject traversal, links, duplicates, and unexpected archive roots."""
    names: set[str] = set()
    total_size = 0
    required_roots = {"db", "static_files", "metadata.json", "summit_checkpoint"}
    found_roots: set[str] = set()

    for member in archive.getmembers():
        path = PurePosixPath(member.name)
        if path.is_absolute() or ".." in path.parts or not path.parts:
            raise CheckpointError(f"Unsafe snapshot archive path: {member.name!r}")
        normalized = str(path)
        if normalized in names:
            raise CheckpointError(f"Duplicate snapshot archive path: {normalized}")
        names.add(normalized)
        root = path.parts[0]
        if root not in required_roots:
            raise CheckpointError(f"Unexpected snapshot archive path: {member.name!r}")
        found_roots.add(root)
        if not (member.isfile() or member.isdir()):
            raise CheckpointError(
                f"Unsupported snapshot archive member type: {member.name!r}"
            )
        if member.isfile():
            total_size += member.size

    missing_roots = required_roots - found_roots
    if missing_roots:
        raise CheckpointError(
            f"Snapshot archive is missing required paths: {', '.join(sorted(missing_roots))}"
        )
    return total_size


def extract_archive(archive_path: Path, destination: Path) -> None:
    """Validate and extract a gzip tar archive into an isolated stage."""
    try:
        with tarfile.open(archive_path, mode="r:gz") as archive:
            total_size = validate_tar_members(archive)
            free_space = shutil.disk_usage(destination).free
            required_space = total_size + total_size // 10
            if free_space < required_space:
                raise CheckpointError(
                    f"Insufficient staging space: need approximately {required_space} bytes, "
                    f"have {free_space} bytes"
                )
            archive.extractall(destination, filter="data")
    except (OSError, tarfile.TarError) as error:
        raise CheckpointError(f"Could not extract snapshot archive: {error}") from error


def validate_extracted_snapshot(stage: Path, manifest: dict[str, Any]) -> Path:
    """Validate the staged Reth/Summit layout and contiguous header history."""
    db = stage / "db"
    static_files = stage / "static_files"
    metadata_path = stage / "metadata.json"
    summit_checkpoint = stage / "summit_checkpoint"

    require_directory(db, "Extracted Reth database directory")
    require_regular_file(db / "mdbx.dat", "Extracted Reth MDBX database")
    require_directory(static_files, "Extracted Reth static-files directory")
    require_directory(summit_checkpoint, "Extracted Summit checkpoint directory")
    for name in ("checkpoint", "last_block", "finalized_header"):
        require_regular_file(
            summit_checkpoint / name,
            f"Extracted Summit checkpoint artifact {name}",
        )

    finalized_headers = summit_checkpoint / "finalized_headers"
    require_directory(finalized_headers, "Extracted finalized-header directory")
    epoch = manifest["epoch"]
    actual_header_epochs: list[int] = []
    for entry in finalized_headers.iterdir():
        require_regular_file(entry, "Extracted finalized-header artifact")
        if not entry.name.isdigit() or str(int(entry.name)) != entry.name:
            raise CheckpointError(f"Unexpected finalized-header filename: {entry.name}")
        actual_header_epochs.append(int(entry.name))
    actual_header_epochs.sort()
    if len(actual_header_epochs) != epoch + 1 or any(
        actual != expected for expected, actual in enumerate(actual_header_epochs)
    ):
        raise CheckpointError(
            f"Finalized-header history must contain exactly epochs 0 through {epoch}; "
            f"found {len(actual_header_epochs)} entries"
        )
    if not _files_equal(
        summit_checkpoint / "finalized_header",
        finalized_headers / str(epoch),
    ):
        raise CheckpointError(
            "Summit finalized_header does not match the terminal finalized_headers entry"
        )

    metadata = read_json(metadata_path, "Embedded snapshot metadata")
    require_exact_keys(
        metadata, {"epoch", "block_number", "timestamp"}, "Embedded metadata"
    )
    if require_int(metadata["epoch"], "Embedded metadata epoch") != epoch:
        raise CheckpointError(
            "Embedded metadata epoch does not match the snapshot manifest"
        )
    block_number = require_int(
        metadata["block_number"], "Embedded metadata block number"
    )
    if not isinstance(metadata["timestamp"], str) or not metadata["timestamp"]:
        raise CheckpointError("Embedded metadata timestamp must be a non-empty string")
    if block_number != manifest["execution"]["block_number"]:
        raise CheckpointError(
            "Embedded metadata block number does not match the execution identity"
        )
    return summit_checkpoint


def _files_equal(left: Path, right: Path) -> bool:
    """Compare two artifacts without reading either whole file into memory."""
    if left.stat().st_size != right.stat().st_size:
        return False
    with left.open("rb") as left_file, right.open("rb") as right_file:
        while True:
            left_chunk = left_file.read(1024 * 1024)
            right_chunk = right_file.read(1024 * 1024)
            if left_chunk != right_chunk:
                return False
            if not left_chunk:
                return True


def load_weak_subjectivity(path: Path, checkpoint_epoch: int) -> dict[str, Any]:
    """Load a recent trusted anchor accepted for the checkpoint epoch."""
    anchor = read_toml(path, "Weak-subjectivity file", root_managed=True)
    require_exact_keys(anchor, {"epoch", "header_digest"}, "Weak-subjectivity file")
    epoch = require_int(anchor["epoch"], "Weak-subjectivity epoch")
    digest = require_digest(anchor["header_digest"], "Weak-subjectivity header digest")
    if epoch > checkpoint_epoch:
        raise CheckpointError(
            f"Weak-subjectivity epoch {epoch} is newer than checkpoint epoch {checkpoint_epoch}"
        )
    if checkpoint_epoch - epoch > WEAK_SUBJECTIVITY_MAX_AGE_EPOCHS:
        raise CheckpointError(
            f"Checkpoint epoch {checkpoint_epoch} is more than "
            f"{WEAK_SUBJECTIVITY_MAX_AGE_EPOCHS} epochs after weak-subjectivity epoch {epoch}"
        )
    return {"epoch": epoch, "header_digest": digest}


# ---------------------------------------------------------------------------
# Quiescence, locking, and atomic filesystem primitives
# ---------------------------------------------------------------------------


def supervisor_program_running(program: str) -> bool:
    """Return whether Supervisor reports a process that may run or resume."""
    try:
        value = supervisor_control.status(program)
    except supervisor_control.SupervisorError as error:
        raise CheckpointError(str(error)) from error
    if not value.exists:
        return False
    return value.state not in supervisor_control.STOPPED_STATES


def require_services_stopped(role: str) -> None:
    """Refuse state mutation while any role-related program may run or resume."""
    running = [
        program
        for program in SERVICE_PROGRAMS[role]
        if supervisor_program_running(program)
    ]
    if running:
        raise CheckpointError(
            f"Refusing to install or roll back while services are running: {', '.join(running)}"
        )


def acquire_summit_lock(summit_data_dir: Path) -> int:
    """Acquire the same non-blocking kernel lock used by Summit programs."""
    summit_metadata = require_directory(summit_data_dir, "Summit data directory")
    lock_file = summit_data_dir / LOCK_FILE_NAME
    try:
        file_descriptor = os.open(
            lock_file,
            os.O_CREAT | os.O_RDWR | os.O_NOFOLLOW,
            0o600,
        )
        lock_metadata = os.fstat(file_descriptor)
        if not stat.S_ISREG(lock_metadata.st_mode):
            os.close(file_descriptor)
            raise CheckpointError(
                f"Summit maintenance lock is not a regular file: {lock_file}"
            )
        fcntl.flock(file_descriptor, fcntl.LOCK_EX | fcntl.LOCK_NB)
        os.fchown(file_descriptor, summit_metadata.st_uid, summit_metadata.st_gid)
        os.fchmod(file_descriptor, 0o600)
    except OSError as error:
        if "file_descriptor" in locals():
            os.close(file_descriptor)
        if error.errno in (errno.EACCES, errno.EAGAIN):
            raise CheckpointError(
                f"Refusing to continue: a Summit process holds {lock_file}"
            ) from error
        raise CheckpointError(
            f"Could not acquire Summit maintenance lock: {error}"
        ) from error
    return file_descriptor


def ensure_root_parent(path: Path, mode: int = 0o755) -> None:
    """Create missing parents below an already secure root path."""
    require_absolute(path, "Root-managed path")
    parent = path.parent
    existing = parent
    while True:
        try:
            existing.lstat()
            break
        except FileNotFoundError:
            if existing == Path("/"):
                raise CheckpointError(f"Could not find an existing parent for {path}")
            existing = existing.parent
    require_secure_root_parent(existing / "placeholder", "Root-managed path")
    parent.mkdir(parents=True, exist_ok=True, mode=mode)
    require_secure_root_parent(path, "Root-managed path")


def atomic_write(path: Path, data: bytes, mode: int) -> None:
    """Durably replace a root-owned file without exposing partial contents."""
    ensure_root_parent(path)
    file_descriptor, temporary_name = tempfile.mkstemp(
        prefix=f".{path.name}.", dir=path.parent
    )
    temporary = Path(temporary_name)
    try:
        with os.fdopen(file_descriptor, "wb") as file:
            file.write(data)
            file.flush()
            os.fsync(file.fileno())
        os.chown(temporary, 0, 0)
        os.chmod(temporary, mode)
        os.replace(temporary, path)
    except Exception:
        temporary.unlink(missing_ok=True)
        raise


def move_path(source: Path, destination: Path) -> None:
    """Move state with rename(2); cross-filesystem copying is not permitted."""
    destination.parent.mkdir(parents=True, exist_ok=True)
    if destination.exists() or destination.is_symlink():
        raise CheckpointError(f"Move destination already exists: {destination}")
    try:
        os.rename(source, destination)
    except OSError as error:
        raise CheckpointError(
            f"Could not atomically move {source} to {destination}: {error}"
        ) from error


def remove_path(path: Path) -> None:
    """Remove a file, symlink, or directory when it exists."""
    if path.is_symlink() or path.is_file():
        path.unlink()
    elif path.is_dir():
        shutil.rmtree(path)


def chown_tree(path: Path, uid: int, gid: int) -> None:
    """Apply preserved service ownership without following symlinks."""
    os.chown(path, uid, gid, follow_symlinks=False)
    if path.is_dir():
        for root, directories, files in os.walk(path, followlinks=False):
            for name in directories:
                os.chown(Path(root) / name, uid, gid, follow_symlinks=False)
            for name in files:
                os.chown(Path(root) / name, uid, gid, follow_symlinks=False)


def mode_bits(metadata: os.stat_result) -> int:
    """Extract permission bits for recording and restoring file modes."""
    return stat.S_IMODE(metadata.st_mode)


def validate_existing_replacement_targets(
    inventory: dict[str, Any],
    checkpoint_path: Path,
    checkpoint_config_path: Path,
    weak_subjectivity_path: Path,
) -> None:
    """Validate every existing path that the transaction may replace."""
    reth_data = inventory["reth_data_dir"]
    for name in ("db", "static_files"):
        path = reth_data / name
        if path.exists() or path.is_symlink():
            require_directory(path, f"Existing Reth {name}")
    if checkpoint_path.exists() or checkpoint_path.is_symlink():
        require_directory(checkpoint_path, "Existing checkpoint path")
    if checkpoint_config_path.exists() or checkpoint_config_path.is_symlink():
        require_root_managed_file(
            checkpoint_config_path, "Existing checkpoint-start config"
        )
    if weak_subjectivity_path.exists() or weak_subjectivity_path.is_symlink():
        require_root_managed_file(
            weak_subjectivity_path, "Existing weak-subjectivity file"
        )


# ---------------------------------------------------------------------------
# Rollback transaction and receipt handling
# ---------------------------------------------------------------------------


def create_backup(
    role: str,
    epoch: int,
    inventory_path: Path,
    inventory: dict[str, Any],
    checkpoint_path: Path,
    weak_subjectivity_path: Path,
    backup_root: Path,
) -> tuple[Path, dict[str, Any]]:
    """Create an ``installing`` receipt before any live state is moved."""
    timestamp = dt.datetime.now(dt.UTC).strftime("%Y%m%dT%H%M%SZ")
    backup = backup_root / f"{role}-epoch-{epoch}-{timestamp}"
    ensure_root_parent(backup_root / "placeholder", mode=0o700)
    backup_root_metadata = require_directory(backup_root, "Checkpoint rollback root")
    if backup_root_metadata.st_uid != 0 or backup_root_metadata.st_mode & 0o077:
        raise CheckpointError(
            f"Checkpoint rollback root must be root-owned with no group or world "
            f"permissions: {backup_root}"
        )
    require_secure_root_parent(backup, "Checkpoint rollback root")
    backup.mkdir(mode=0o700)

    reth_metadata = inventory["reth_data_dir"].stat()
    summit_metadata = inventory["summit_data_dir"].stat()
    receipt: dict[str, Any] = {
        "version": 1,
        "state": "installing",
        "role": role,
        "epoch": epoch,
        "created_at": dt.datetime.now(dt.UTC).isoformat(),
        "updated_at": dt.datetime.now(dt.UTC).isoformat(),
        "hostname": os.uname().nodename,
        "inventory_path": str(inventory_path),
        "paths": {
            "reth_data_dir": str(inventory["reth_data_dir"]),
            "summit_data_dir": str(inventory["summit_data_dir"]),
            "summit_keys_dir": str(inventory["summit_keys_dir"]),
            "checkpoint_path": str(checkpoint_path),
            "checkpoint_config_path": str(CHECKPOINT_CONFIG_PATHS[role]),
            "weak_subjectivity_path": str(weak_subjectivity_path),
        },
        "ownership": {
            "reth": {
                "uid": reth_metadata.st_uid,
                "gid": reth_metadata.st_gid,
                "mode": mode_bits(reth_metadata),
            },
            "summit": {
                "uid": summit_metadata.st_uid,
                "gid": summit_metadata.st_gid,
                "mode": mode_bits(summit_metadata),
            },
        },
        "previous": {
            "reth_db": (inventory["reth_data_dir"] / "db").exists(),
            "reth_static_files": (inventory["reth_data_dir"] / "static_files").exists(),
            "checkpoint": checkpoint_path.exists(),
            "checkpoint_config": CHECKPOINT_CONFIG_PATHS[role].exists(),
            "weak_subjectivity": weak_subjectivity_path.exists(),
        },
    }
    atomic_write(
        backup / RECEIPT_FILE_NAME,
        json.dumps(receipt, indent=2).encode() + b"\n",
        0o600,
    )
    return backup, receipt


def backup_current_state(
    backup: Path,
    receipt: dict[str, Any],
    inventory: dict[str, Any],
) -> None:
    """Atomically move all replaceable Reth and Summit state into the backup."""
    reth_data = Path(receipt["paths"]["reth_data_dir"])
    for name, key in (("db", "reth_db"), ("static_files", "reth_static_files")):
        source = reth_data / name
        if receipt["previous"][key]:
            require_directory(source, f"Existing Reth {name}")
            move_path(source, backup / "reth" / name)

    summit_data = Path(receipt["paths"]["summit_data_dir"])
    summit_backup = backup / "summit-data"
    summit_backup.mkdir(mode=0o700)
    for child in summit_data.iterdir():
        if child.name == LOCK_FILE_NAME:
            continue
        move_path(child, summit_backup / child.name)

    checkpoint_path = Path(receipt["paths"]["checkpoint_path"])
    if receipt["previous"]["checkpoint"]:
        require_directory(checkpoint_path, "Existing checkpoint path")
        move_path(checkpoint_path, backup / "previous-checkpoint")

    config_path = Path(receipt["paths"]["checkpoint_config_path"])
    if receipt["previous"]["checkpoint_config"]:
        require_root_managed_file(config_path, "Existing checkpoint-start config")
        shutil.copy2(config_path, backup / "previous-checkpoint-start.toml")

    weak_path = Path(receipt["paths"]["weak_subjectivity_path"])
    if receipt["previous"]["weak_subjectivity"]:
        require_root_managed_file(weak_path, "Existing weak-subjectivity file")
        shutil.copy2(weak_path, backup / "previous-weak-subjectivity.toml")

    # Keep the key directory entirely outside the mutable-state transaction.
    require_directory(inventory["summit_keys_dir"], "Summit keys directory")


def install_new_state(
    stage: Path,
    receipt: dict[str, Any],
    anchor_source: Path,
) -> None:
    """Move staged state into place and write root-managed startup inputs."""
    reth_data = Path(receipt["paths"]["reth_data_dir"])
    reth_owner = receipt["ownership"]["reth"]
    for name in ("db", "static_files"):
        source = stage / name
        destination = reth_data / name
        move_path(source, destination)
        chown_tree(destination, reth_owner["uid"], reth_owner["gid"])

    checkpoint_source = stage / "summit_checkpoint"
    checkpoint_path = Path(receipt["paths"]["checkpoint_path"])
    checkpoint_path.parent.mkdir(parents=True, exist_ok=True)
    move_path(checkpoint_source, checkpoint_path)
    summit_owner = receipt["ownership"]["summit"]
    chown_tree(checkpoint_path, summit_owner["uid"], summit_owner["gid"])

    weak_path = Path(receipt["paths"]["weak_subjectivity_path"])
    atomic_write(weak_path, anchor_source.read_bytes(), 0o644)

    checkpoint_config = Path(receipt["paths"]["checkpoint_config_path"])
    config_contents = (
        f"checkpoint_path = {json.dumps(str(checkpoint_path))}\n"
        f"weak_subjectivity_path = {json.dumps(str(weak_path))}\n"
    ).encode()
    atomic_write(checkpoint_config, config_contents, 0o644)


def restore_backup(
    backup: Path,
    receipt: dict[str, Any],
    *,
    remove_new_summit_state: bool,
) -> None:
    """Restore only receipt-backed state, including interrupted installations."""
    reth_data = Path(receipt["paths"]["reth_data_dir"])
    for name, key in (("db", "reth_db"), ("static_files", "reth_static_files")):
        backup_path = backup / "reth" / name
        destination = reth_data / name
        if backup_path.exists():
            remove_path(destination)
            move_path(backup_path, destination)
        elif not receipt["previous"][key]:
            remove_path(destination)

    summit_data = Path(receipt["paths"]["summit_data_dir"])
    if remove_new_summit_state:
        for child in list(summit_data.iterdir()):
            if child.name != LOCK_FILE_NAME:
                remove_path(child)
    summit_backup = backup / "summit-data"
    if summit_backup.exists():
        for child in list(summit_backup.iterdir()):
            destination = summit_data / child.name
            if destination.exists() or destination.is_symlink():
                raise CheckpointError(
                    f"Cannot restore Summit backup over existing path: {destination}"
                )
            move_path(child, destination)

    checkpoint_path = Path(receipt["paths"]["checkpoint_path"])
    previous_checkpoint = backup / "previous-checkpoint"
    if previous_checkpoint.exists():
        remove_path(checkpoint_path)
        move_path(previous_checkpoint, checkpoint_path)
    elif not receipt["previous"]["checkpoint"]:
        remove_path(checkpoint_path)

    config_path = Path(receipt["paths"]["checkpoint_config_path"])
    previous_config = backup / "previous-checkpoint-start.toml"
    if previous_config.exists():
        atomic_write(
            config_path,
            previous_config.read_bytes(),
            mode_bits(previous_config.stat()),
        )
    elif not receipt["previous"]["checkpoint_config"]:
        remove_path(config_path)

    weak_path = Path(receipt["paths"]["weak_subjectivity_path"])
    previous_weak_subjectivity = backup / "previous-weak-subjectivity.toml"
    if previous_weak_subjectivity.exists():
        atomic_write(
            weak_path,
            previous_weak_subjectivity.read_bytes(),
            mode_bits(previous_weak_subjectivity.stat()),
        )
    elif not receipt["previous"]["weak_subjectivity"]:
        remove_path(weak_path)


def update_receipt(backup: Path, receipt: dict[str, Any], state: str) -> None:
    """Durably advance the rollback receipt's transaction state."""
    receipt["state"] = state
    receipt["updated_at"] = dt.datetime.now(dt.UTC).isoformat()
    atomic_write(
        backup / RECEIPT_FILE_NAME,
        json.dumps(receipt, indent=2).encode() + b"\n",
        0o600,
    )


def validate_rollback_receipt(receipt: dict[str, Any]) -> str:
    """Validate every path, ownership record, and prior-state flag in a receipt."""
    require_exact_keys(
        receipt,
        {
            "version",
            "state",
            "role",
            "epoch",
            "created_at",
            "hostname",
            "inventory_path",
            "paths",
            "ownership",
            "previous",
            "updated_at",
        },
        "Rollback manifest",
    )
    if receipt["version"] != 1:
        raise CheckpointError(
            f"Unsupported rollback manifest version: {receipt['version']!r}"
        )
    role = receipt["role"]
    if role not in SERVICE_PROGRAMS:
        raise CheckpointError(f"Invalid rollback role: {role!r}")
    require_int(receipt["epoch"], "Rollback epoch")
    if not isinstance(receipt["hostname"], str) or not receipt["hostname"]:
        raise CheckpointError("Rollback hostname must be a non-empty string")

    paths = receipt["paths"]
    if not isinstance(paths, dict):
        raise CheckpointError("Rollback paths must be an object")
    require_exact_keys(
        paths,
        {
            "reth_data_dir",
            "summit_data_dir",
            "summit_keys_dir",
            "checkpoint_path",
            "checkpoint_config_path",
            "weak_subjectivity_path",
        },
        "Rollback paths",
    )
    parsed_paths: dict[str, Path] = {}
    for key, value in paths.items():
        if not isinstance(value, str) or not value:
            raise CheckpointError(f"Rollback path {key} must be a non-empty string")
        parsed_path = Path(value)
        require_absolute(parsed_path, f"Rollback path {key}")
        parsed_paths[key] = parsed_path
    if parsed_paths["checkpoint_config_path"] != CHECKPOINT_CONFIG_PATHS[role]:
        raise CheckpointError("Rollback checkpoint config path does not match the role")
    require_separate_paths(
        {
            "Reth data directory": parsed_paths["reth_data_dir"],
            "Summit data directory": parsed_paths["summit_data_dir"],
            "Summit keys directory": parsed_paths["summit_keys_dir"],
            "Checkpoint destination": parsed_paths["checkpoint_path"],
            "Checkpoint-start config": parsed_paths["checkpoint_config_path"],
            "Weak-subjectivity path": parsed_paths["weak_subjectivity_path"],
        }
    )

    ownership = receipt["ownership"]
    if not isinstance(ownership, dict):
        raise CheckpointError("Rollback ownership must be an object")
    require_exact_keys(ownership, {"reth", "summit"}, "Rollback ownership")
    for name in ("reth", "summit"):
        value = ownership[name]
        if not isinstance(value, dict):
            raise CheckpointError(f"Rollback {name} ownership must be an object")
        require_exact_keys(value, {"uid", "gid", "mode"}, f"Rollback {name} ownership")
        for key in ("uid", "gid", "mode"):
            require_int(value[key], f"Rollback {name} {key}")

    previous = receipt["previous"]
    if not isinstance(previous, dict):
        raise CheckpointError("Rollback previous-state flags must be an object")
    require_exact_keys(
        previous,
        {
            "reth_db",
            "reth_static_files",
            "checkpoint",
            "checkpoint_config",
            "weak_subjectivity",
        },
        "Rollback previous-state flags",
    )
    if any(not isinstance(value, bool) for value in previous.values()):
        raise CheckpointError("Rollback previous-state flags must be booleans")
    return role


def print_rollback_paths(backup: Path) -> None:
    """Show operators the recovery assets that must be preserved."""
    print(f"Rollback backup: {backup}")
    for relative in (
        Path("reth/db"),
        Path("reth/static_files"),
        Path("summit-data"),
        Path("previous-checkpoint"),
        Path("previous-checkpoint-start.toml"),
        Path("previous-weak-subjectivity.toml"),
        Path(RECEIPT_FILE_NAME),
    ):
        path = backup / relative
        if path.exists():
            print(f"  {path}")


def confirm_action(question: str, args: argparse.Namespace, refusal: str) -> None:
    """Ask one yes/no question; non-interactive use requires ``--yes``."""
    if args.yes:
        return
    if not sys.stdin.isatty():
        raise CheckpointError(
            "Interactive confirmation is unavailable; pass --yes to confirm"
        )
    try:
        response = input(f"{question} [y/N] ").strip().lower()
    except EOFError as error:
        raise CheckpointError(
            "Interactive confirmation is unavailable; pass --yes to confirm"
        ) from error
    if response not in {"y", "yes"}:
        raise CheckpointError(refusal)


def confirm_install(hostname: str, epoch: int, args: argparse.Namespace) -> None:
    """Confirm state replacement on this host before it begins."""
    print("This will replace the configured Reth and Summit mutable state.")
    confirm_action(
        f"Install the epoch {epoch} checkpoint on {hostname}?",
        args,
        "Checkpoint installation was not confirmed",
    )


def confirm_rollback(hostname: str, backup: Path, args: argparse.Namespace) -> None:
    """Confirm state restoration on this host before it begins."""
    print("This will restore the backed-up Reth and Summit mutable state.")
    confirm_action(
        f"Roll back {backup.name} on {hostname}?",
        args,
        "Checkpoint rollback was not confirmed",
    )


# ---------------------------------------------------------------------------
# Public checkpoint operations called by the unified CLI
# ---------------------------------------------------------------------------


def install_checkpoint(args: argparse.Namespace) -> Path:
    """Verify, back up, and atomically install matching Reth/Summit state."""
    require_root()
    role = args.role
    inventory_path = args.inventory or DEFAULT_INVENTORY_PATHS[role]
    require_absolute(inventory_path, "Installation inventory path")
    inventory = load_inventory(role, inventory_path)
    manifest = load_manifest(args.manifest)
    verify_archive_identity(args.archive, manifest)
    anchor = load_weak_subjectivity(args.weak_subjectivity_path, manifest["epoch"])

    checkpoint_path = args.checkpoint_path or (
        inventory["summit_data_dir"].parent / "checkpoint-start" / "summit_checkpoint"
    )
    weak_path = args.installed_weak_subjectivity_path
    backup_root = args.backup_root or (
        inventory["reth_data_dir"].parent / "checkpoint-rollback"
    )
    for path, description in (
        (checkpoint_path, "Checkpoint destination"),
        (weak_path, "Installed weak-subjectivity path"),
        (backup_root, "Checkpoint rollback root"),
    ):
        require_absolute(path, description)

    require_separate_paths(
        {
            "Reth data directory": inventory["reth_data_dir"],
            "Summit data directory": inventory["summit_data_dir"],
            "Summit keys directory": inventory["summit_keys_dir"],
            "Checkpoint destination": checkpoint_path,
            "Checkpoint-start config": CHECKPOINT_CONFIG_PATHS[role],
            "Weak-subjectivity path": weak_path,
            "Checkpoint rollback root": backup_root,
        }
    )
    require_shared_state_filesystem(
        {
            "Reth data directory": inventory["reth_data_dir"],
            "Summit data directory": inventory["summit_data_dir"],
            "Checkpoint destination": checkpoint_path,
            "Checkpoint rollback root": backup_root,
        }
    )
    validate_existing_replacement_targets(
        inventory,
        checkpoint_path,
        CHECKPOINT_CONFIG_PATHS[role],
        weak_path,
    )

    stage = Path(
        tempfile.mkdtemp(
            prefix=".seismic-checkpoint-stage.",
            dir=inventory["reth_data_dir"].parent,
        )
    )
    backup: Path | None = None
    receipt: dict[str, Any] | None = None
    lock_file_descriptor: int | None = None
    try:
        extract_archive(args.archive, stage)
        validate_extracted_snapshot(stage, manifest)
        # Summit performs the cryptographic digest/signature/anchor verification.
        print(f"Verified snapshot archive and manifest for epoch {manifest['epoch']}.")
        print(
            f"Weak-subjectivity anchor: epoch {anchor['epoch']} "
            f"({anchor['header_digest']})"
        )
        print(f"Reth target: {inventory['reth_data_dir']}")
        print(f"Summit mutable store: {inventory['summit_data_dir']}")
        print(f"Summit checkpoint target: {checkpoint_path}")
        confirm_install(os.uname().nodename, manifest["epoch"], args)

        # Check both before and after taking the shared Summit lock. This closes
        # the window in which a process could start between the first status check
        # and the maintenance transaction.
        require_services_stopped(role)
        lock_file_descriptor = acquire_summit_lock(inventory["summit_data_dir"])
        require_services_stopped(role)

        backup, receipt = create_backup(
            role,
            manifest["epoch"],
            inventory_path,
            inventory,
            checkpoint_path,
            weak_path,
            backup_root,
        )
        print(f"Rollback backup: {backup}")
        print(f"Rollback receipt: {backup / RECEIPT_FILE_NAME}")
        backup_current_state(backup, receipt, inventory)
        print_rollback_paths(backup)
        install_new_state(stage, receipt, args.weak_subjectivity_path)
        update_receipt(backup, receipt, "installed")
    except Exception:
        if backup is not None and receipt is not None:
            print(
                f"Checkpoint installation failed. Rollback backup: {backup}",
                file=sys.stderr,
            )
            try:
                restore_backup(backup, receipt, remove_new_summit_state=False)
                update_receipt(backup, receipt, "automatically-rolled-back")
                print(
                    "Previous node state was restored automatically.", file=sys.stderr
                )
            # Report any rollback failure without hiding the original install error.
            except Exception as rollback_error:  # noqa: BLE001
                print(
                    f"Automatic rollback also failed: {rollback_error}", file=sys.stderr
                )
                print(
                    f"Preserve {backup} and use the rollback command after inspection.",
                    file=sys.stderr,
                )
        raise
    finally:
        if lock_file_descriptor is not None:
            os.close(lock_file_descriptor)
        shutil.rmtree(stage, ignore_errors=True)

    assert backup is not None
    print(f"Installed epoch {manifest['epoch']} checkpoint. Services remain stopped.")
    print_rollback_paths(backup)
    print("Rollback command:")
    cli = Path(__file__).resolve().parents[1] / "seismic-node.py"
    print(
        f"  sudo {shlex.quote(str(cli))} checkpoint rollback "
        f"--backup {shlex.quote(str(backup))}"
    )
    print(
        "Start the role-specific services only after reviewing Supervisor configuration."
    )
    return backup


def discover_backup_roots() -> list[Path]:
    """Derive the default rollback roots from the installed inventories."""
    roots: list[Path] = []
    for role, inventory_path in DEFAULT_INVENTORY_PATHS.items():
        try:
            inventory = load_inventory(role, inventory_path)
        except CheckpointError:
            continue
        root = inventory["reth_data_dir"].parent / "checkpoint-rollback"
        if root not in roots and root.is_dir() and not root.is_symlink():
            roots.append(root)
    return roots


def describe_backup(backup: Path) -> str:
    """Summarize one backup for interactive selection, tolerating bad receipts."""
    try:
        receipt = read_json(
            backup / RECEIPT_FILE_NAME, "Rollback manifest", root_managed=True
        )
        role = receipt.get("role", "?")
        epoch = receipt.get("epoch", "?")
        state = receipt.get("state", "?")
        created = receipt.get("created_at", "?")
        return (
            f"{backup.name}  role={role} epoch={epoch} state={state} created={created}"
        )
    except CheckpointError as error:
        return f"{backup.name}  (unreadable receipt: {error})"


def select_backup(args: argparse.Namespace, action: str) -> Path:
    """Return the requested backup or ask the operator to choose one."""
    if args.backup is not None:
        return args.backup
    if not sys.stdin.isatty():
        raise CheckpointError(f"Non-interactive {action} requires --backup")

    candidates: list[Path] = []
    for root in discover_backup_roots():
        for entry in root.iterdir():
            if entry.is_symlink() or not entry.is_dir():
                continue
            if not (entry / RECEIPT_FILE_NAME).is_file():
                continue
            candidates.append(entry)
    if not candidates:
        raise CheckpointError(
            "No rollback backups were found under the default rollback roots; "
            "provide --backup"
        )
    # The timestamp suffix makes name order chronological; newest first.
    candidates.sort(key=lambda path: path.name, reverse=True)

    print("Available rollback backups (newest first):")
    for index, candidate in enumerate(candidates, start=1):
        print(f"  [{index}] {describe_backup(candidate)}")
    while True:
        try:
            response = input(
                f"Select a backup to {action} [1-{len(candidates)}], "
                "or press Enter to abort: "
            ).strip()
        except EOFError as error:
            raise CheckpointError(
                "Interactive selection is unavailable; provide --backup"
            ) from error
        if not response:
            raise CheckpointError(f"Backup {action} aborted; no backup was selected")
        if response.isdigit() and 1 <= int(response) <= len(candidates):
            return candidates[int(response) - 1]
        print(f"Enter a number from 1 to {len(candidates)}, or press Enter to abort.")


def rollback_checkpoint(args: argparse.Namespace) -> None:
    """Restore a matching receipt-backed backup while services are stopped."""
    require_root()
    backup = select_backup(args, "roll back")
    require_absolute(backup, "Rollback backup")
    require_directory(backup, "Rollback backup")
    receipt_path = backup / RECEIPT_FILE_NAME
    receipt = read_json(receipt_path, "Rollback manifest", root_managed=True)
    role = validate_rollback_receipt(receipt)
    require_shared_state_filesystem(
        {
            "Reth data directory": Path(receipt["paths"]["reth_data_dir"]),
            "Summit data directory": Path(receipt["paths"]["summit_data_dir"]),
            "Checkpoint destination": Path(receipt["paths"]["checkpoint_path"]),
            "Rollback backup": backup,
        }
    )
    if receipt["state"] not in {"installed", "installing"}:
        raise CheckpointError(
            f"Rollback backup is not in a rollback-eligible state: {receipt['state']!r}"
        )
    partial_install = receipt["state"] == "installing"
    if partial_install:
        print(
            "Warning: this receipt records an interrupted installation; rollback will "
            "restore only paths that reached the backup directory.",
            file=sys.stderr,
        )
    hostname = os.uname().nodename
    if receipt["hostname"] != hostname:
        raise CheckpointError(
            f"Rollback backup belongs to host {receipt['hostname']!r}, not {hostname!r}"
        )
    confirm_rollback(hostname, backup, args)
    require_services_stopped(role)
    summit_data = Path(receipt["paths"]["summit_data_dir"])
    lock_file_descriptor = acquire_summit_lock(summit_data)
    try:
        require_services_stopped(role)
        restore_backup(
            backup,
            receipt,
            remove_new_summit_state=not partial_install,
        )
        update_receipt(backup, receipt, "rolled-back")
    finally:
        os.close(lock_file_descriptor)

    print("Checkpoint rollback completed. Services remain stopped.")
    print(f"Rollback receipt: {receipt_path}")


def validate_checkpoint_start_configuration(role: str) -> tuple[Path, Path]:
    """Revalidate installed checkpoint and anchor files immediately before start."""
    config_path = CHECKPOINT_CONFIG_PATHS[role]
    config = read_toml(config_path, "Checkpoint-start configuration", root_managed=True)
    require_exact_keys(
        config,
        {"checkpoint_path", "weak_subjectivity_path"},
        "Checkpoint-start configuration",
    )
    paths: list[Path] = []
    for key in ("checkpoint_path", "weak_subjectivity_path"):
        value = config[key]
        if not isinstance(value, str) or not value:
            raise CheckpointError(
                f"Checkpoint-start configuration {key} must be a non-empty string"
            )
        path = Path(value)
        require_absolute(path, f"Checkpoint-start configuration {key}")
        paths.append(path)
    checkpoint_path, weak_subjectivity_path = paths
    require_directory(checkpoint_path, "Installed Summit checkpoint directory")
    for name in ("checkpoint", "last_block", "finalized_header"):
        require_regular_file(
            checkpoint_path / name,
            f"Installed Summit checkpoint artifact {name}",
        )
    headers = checkpoint_path / "finalized_headers"
    require_directory(headers, "Installed finalized-header directory")
    epochs: list[int] = []
    for entry in headers.iterdir():
        require_regular_file(entry, "Installed finalized-header artifact")
        if not entry.name.isdigit() or str(int(entry.name)) != entry.name:
            raise CheckpointError(
                f"Unexpected installed finalized-header filename: {entry.name}"
            )
        epochs.append(int(entry.name))
    epochs.sort()
    if not epochs or any(actual != expected for expected, actual in enumerate(epochs)):
        raise CheckpointError(
            "Installed finalized-header history must be contiguous from epoch 0"
        )
    terminal_epoch = epochs[-1]
    if not _files_equal(
        checkpoint_path / "finalized_header",
        headers / str(terminal_epoch),
    ):
        raise CheckpointError(
            "Installed finalized_header does not match its terminal history entry"
        )
    load_weak_subjectivity(weak_subjectivity_path, terminal_epoch)
    return checkpoint_path, weak_subjectivity_path


def directory_size(path: Path) -> int:
    """Measure a backup without following links outside its directory tree."""
    total = 0
    for root, directories, files in os.walk(path, followlinks=False):
        for name in directories:
            entry = Path(root) / name
            if entry.is_symlink():
                total += entry.lstat().st_size
        for name in files:
            total += (Path(root) / name).lstat().st_size
    return total


def human_size(value: int) -> str:
    """Format a byte count for an operator confirmation message."""
    units = ("B", "KiB", "MiB", "GiB", "TiB")
    amount = float(value)
    for unit in units:
        if amount < 1024 or unit == units[-1]:
            return f"{amount:.1f} {unit}"
        amount /= 1024
    return f"{value} B"


def confirm_delete_backup(
    backup: Path,
    hostname: str,
    args: argparse.Namespace,
) -> None:
    """Confirm permanent rollback-backup deletion before it begins."""
    confirm_action(
        f"Permanently delete {backup.name} on {hostname}?",
        args,
        "Rollback-backup deletion was not confirmed",
    )


def delete_backup(args: argparse.Namespace) -> None:
    """Permanently delete an explicitly confirmed, valid rollback backup."""
    require_root()
    backup = select_backup(args, "delete")
    require_absolute(backup, "Rollback backup")
    backup_metadata = require_directory(backup, "Rollback backup")
    if backup_metadata.st_uid != 0 or backup_metadata.st_mode & 0o077:
        raise CheckpointError(
            f"Rollback backup must be root-owned with no group or world permissions: {backup}"
        )
    receipt_path = backup / RECEIPT_FILE_NAME
    receipt = read_json(receipt_path, "Rollback manifest", root_managed=True)
    validate_rollback_receipt(receipt)
    if receipt["state"] == "installing":
        raise CheckpointError(
            "Refusing to delete an interrupted-install backup; it may be the only recovery copy"
        )
    if receipt["state"] not in {
        "installed",
        "rolled-back",
        "automatically-rolled-back",
    }:
        raise CheckpointError(
            f"Rollback backup has an unsupported deletion state: {receipt['state']!r}"
        )
    hostname = os.uname().nodename
    if receipt["hostname"] != hostname:
        raise CheckpointError(
            f"Rollback backup belongs to host {receipt['hostname']!r}, not {hostname!r}"
        )
    live_paths = {
        name: Path(value)
        for name, value in receipt["paths"].items()
        if name
        in {
            "reth_data_dir",
            "summit_data_dir",
            "summit_keys_dir",
            "checkpoint_path",
        }
    }
    require_separate_paths({"Rollback backup": backup, **live_paths})
    size = directory_size(backup)
    print(f"Rollback backup: {backup}")
    print(f"Receipt state: {receipt['state']}")
    print(f"Backup size: {human_size(size)}")
    if receipt["state"] == "installed":
        print(
            "Warning: deleting this backup permanently removes the ability to roll "
            "back to the pre-checkpoint node state.",
            file=sys.stderr,
        )
    confirm_delete_backup(backup, hostname, args)
    deleting = backup.parent / f".{backup.name}.deleting-{os.getpid()}"
    if deleting.exists() or deleting.is_symlink():
        raise CheckpointError(f"Temporary deletion path already exists: {deleting}")
    # Rename first so an interrupted recursive deletion cannot leave a directory
    # that still appears to be a complete, usable rollback backup.
    os.rename(backup, deleting)
    try:
        shutil.rmtree(deleting)
    except OSError as error:
        raise CheckpointError(
            f"Backup deletion was incomplete; remaining data is at {deleting}: {error}"
        ) from error
    print(f"Deleted rollback backup: {backup}")

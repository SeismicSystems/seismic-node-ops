#!/usr/bin/python3

"""Start Summit with verified checkpoint paths and a mutual-exclusion lock."""

from __future__ import annotations

import argparse
import errno
import fcntl
import os
import stat
import sys
import tomllib
from pathlib import Path
from typing import NoReturn

# The checkpoint installer writes this small runtime config. Keeping the schema
# exact prevents misspelled settings from being silently ignored.
EXPECTED_CONFIG_KEYS = {"checkpoint_path", "weak_subjectivity_path"}

# These options are controlled by this runner. In particular, the unsafe flags
# must never be smuggled into the base command rendered by the installer.
FORBIDDEN_COMMAND_OPTIONS = {
    "--checkpoint-path",
    "--weak-subjectivity-path",
    "--checkpoint-or-default",
    "--unsafe-skip-checkpoint-verification",
}


class RunnerError(Exception):
    """An operator-actionable checkpoint runner error."""


def parse_args() -> argparse.Namespace:
    """Parse runner options and preserve the role-specific command after `--`."""

    parser = argparse.ArgumentParser(
        description="Start Summit with checkpoint paths from a runtime TOML file.",
        allow_abbrev=False,
    )
    parser.add_argument("--config", required=True, type=Path)
    parser.add_argument("--lock-file", required=True, type=Path)
    parser.add_argument("command", nargs=argparse.REMAINDER)
    args = parser.parse_args()

    if args.command and args.command[0] == "--":
        args.command = args.command[1:]
    if not args.command:
        parser.error("a command is required after --")
    if not Path(args.command[0]).is_absolute():
        parser.error("the command executable must be an absolute path")

    return args


def require_absolute(path: Path, description: str) -> None:
    if not path.is_absolute():
        raise RunnerError(f"{description} must be an absolute path: {path}")


def require_directory(path: Path, description: str) -> os.stat_result:
    """Require a real directory without following a final-component symlink."""

    require_absolute(path, description)
    try:
        metadata = path.lstat()
    except FileNotFoundError as error:
        raise RunnerError(f"{description} is missing: {path}") from error
    except OSError as error:
        raise RunnerError(
            f"Could not inspect {description}: {path}: {error}"
        ) from error
    if stat.S_ISLNK(metadata.st_mode) or not stat.S_ISDIR(metadata.st_mode):
        raise RunnerError(f"{description} is not a safe directory: {path}")
    return metadata


def require_regular_file(
    path: Path,
    description: str,
    *,
    require_nonempty: bool = True,
) -> os.stat_result:
    """Require a regular, non-symlinked file and normally require content."""

    require_absolute(path, description)
    try:
        metadata = path.lstat()
    except FileNotFoundError as error:
        raise RunnerError(f"{description} is missing: {path}") from error
    except OSError as error:
        raise RunnerError(
            f"Could not inspect {description}: {path}: {error}"
        ) from error
    if stat.S_ISLNK(metadata.st_mode) or not stat.S_ISREG(metadata.st_mode):
        raise RunnerError(f"{description} is not a safe regular file: {path}")
    if require_nonempty and metadata.st_size == 0:
        raise RunnerError(f"{description} is empty: {path}")
    return metadata


def require_root_managed_file(path: Path, description: str) -> os.stat_result:
    """Require a root-owned regular file that unprivileged users cannot modify."""

    metadata = require_regular_file(path, description)
    if metadata.st_uid != 0 or metadata.st_mode & 0o022:
        raise RunnerError(
            f"{description} must be root-owned and not group- or world-writable: {path}"
        )
    return metadata


def load_config(config_path: Path) -> tuple[Path, Path]:
    """Load the two runner-managed paths from a strict TOML document."""

    require_root_managed_file(config_path, "Checkpoint startup config")

    # Reopen with O_NOFOLLOW after the initial diagnostics-friendly check so a
    # final-component symlink swap cannot redirect the runner to another file.
    flags = os.O_RDONLY | os.O_NOFOLLOW
    try:
        file_descriptor = os.open(config_path, flags)
    except OSError as error:
        raise RunnerError(
            f"Could not safely open checkpoint startup config: {config_path}: {error}"
        ) from error

    try:
        with os.fdopen(file_descriptor, "rb") as config_file:
            metadata = os.fstat(config_file.fileno())
            if not stat.S_ISREG(metadata.st_mode) or metadata.st_size == 0:
                raise RunnerError(
                    f"Checkpoint startup config is not a non-empty regular file: "
                    f"{config_path}"
                )
            config = tomllib.load(config_file)
    except tomllib.TOMLDecodeError as error:
        raise RunnerError(
            f"Checkpoint startup config is not valid TOML: {config_path}: {error}"
        ) from error

    keys = set(config)
    if keys != EXPECTED_CONFIG_KEYS:
        missing = sorted(EXPECTED_CONFIG_KEYS - keys)
        unexpected = sorted(keys - EXPECTED_CONFIG_KEYS)
        details = []
        if missing:
            details.append(f"missing keys: {', '.join(missing)}")
        if unexpected:
            details.append(f"unexpected keys: {', '.join(unexpected)}")
        raise RunnerError(
            f"Checkpoint startup config has an invalid schema: {config_path} "
            f"({'; '.join(details)})"
        )

    for key in EXPECTED_CONFIG_KEYS:
        if not isinstance(config[key], str) or not config[key]:
            raise RunnerError(
                f"Checkpoint startup config {key} must be a non-empty string: "
                f"{config_path}"
            )

    checkpoint_path = Path(config["checkpoint_path"])
    weak_subjectivity_path = Path(config["weak_subjectivity_path"])
    require_absolute(checkpoint_path, "Summit checkpoint directory")
    require_absolute(weak_subjectivity_path, "Weak-subjectivity file")
    return checkpoint_path, weak_subjectivity_path


def validate_checkpoint(checkpoint_path: Path) -> None:
    """Perform structural checks before Summit performs cryptographic checks."""

    require_directory(checkpoint_path, "Summit checkpoint directory")
    for name in ("checkpoint", "last_block", "finalized_header"):
        require_regular_file(
            checkpoint_path / name,
            f"Summit checkpoint artifact {name}",
        )

    finalized_headers = checkpoint_path / "finalized_headers"
    require_directory(finalized_headers, "Summit finalized-header directory")
    try:
        entries = list(finalized_headers.iterdir())
    except OSError as error:
        raise RunnerError(
            f"Could not inspect Summit finalized-header directory: "
            f"{finalized_headers}: {error}"
        ) from error

    usable_header_found = False
    for entry in entries:
        try:
            metadata = entry.lstat()
        except OSError as error:
            raise RunnerError(
                f"Could not inspect Summit finalized-header artifact: {entry}: {error}"
            ) from error
        if (
            entry.name.isdigit()
            and stat.S_ISREG(metadata.st_mode)
            and metadata.st_size > 0
        ):
            usable_header_found = True
            break

    if not usable_header_found:
        raise RunnerError(
            f"Summit finalized-header directory contains no usable headers: "
            f"{finalized_headers}"
        )


def validate_command(command: list[str]) -> None:
    """Keep checkpoint selection and verification policy under runner control."""

    for argument in command:
        for forbidden in FORBIDDEN_COMMAND_OPTIONS:
            if argument == forbidden or argument.startswith(f"{forbidden}="):
                raise RunnerError(
                    f"Summit command already contains forbidden runner-managed option: "
                    f"{argument}"
                )


def acquire_lock(lock_file: Path) -> int:
    """Acquire the lock shared by normal and checkpoint Summit programs."""

    require_absolute(lock_file, "Summit lock file")
    require_directory(lock_file.parent, "Summit lock-file parent")

    flags = os.O_CREAT | os.O_RDWR | os.O_NOFOLLOW
    try:
        file_descriptor = os.open(lock_file, flags, 0o600)
    except OSError as error:
        raise RunnerError(
            f"Could not safely open Summit lock file: {lock_file}: {error}"
        ) from error

    try:
        fcntl.flock(file_descriptor, fcntl.LOCK_EX | fcntl.LOCK_NB)
    except OSError as error:
        os.close(file_descriptor)
        if error.errno in (errno.EACCES, errno.EAGAIN):
            raise RunnerError(
                f"Refusing to start: another Summit program holds {lock_file}"
            ) from error
        raise RunnerError(
            f"Could not lock Summit lock file: {lock_file}: {error}"
        ) from error

    # Python descriptors are non-inheritable by default. Summit must inherit
    # this descriptor so the advisory lock remains held after os.execvpe().
    # fcntl.flock uses the same Linux lock mechanism as /usr/bin/flock, which
    # wraps the normal Summit Supervisor program.
    try:
        os.set_inheritable(file_descriptor, True)
    except OSError as error:
        os.close(file_descriptor)
        raise RunnerError(
            f"Could not preserve the Summit process lock across exec: {error}"
        ) from error
    return file_descriptor


def execute(
    command: list[str],
    checkpoint_path: Path,
    weak_subjectivity_path: Path,
) -> NoReturn:
    """Append verified paths and replace the runner with Summit."""

    # Supervisor supplies all role-specific arguments, including --observer.
    # This shared runner only owns the two checkpoint-import arguments.
    final_command = [
        *command,
        "--checkpoint-path",
        str(checkpoint_path),
        "--weak-subjectivity-path",
        str(weak_subjectivity_path),
    ]
    try:
        os.execvpe(final_command[0], final_command, os.environ)
    except OSError as error:
        raise RunnerError(f"Could not execute Summit command: {error}") from error


def main() -> NoReturn:
    args = parse_args()
    checkpoint_path, weak_subjectivity_path = load_config(args.config)
    validate_checkpoint(checkpoint_path)
    require_root_managed_file(weak_subjectivity_path, "Weak-subjectivity file")
    validate_command(args.command)
    lock_file_descriptor = acquire_lock(args.lock_file)

    try:
        execute(args.command, checkpoint_path, weak_subjectivity_path)
    finally:
        os.close(lock_file_descriptor)


if __name__ == "__main__":
    try:
        main()
    except RunnerError as error:
        print(f"summit-checkpoint-runner: {error}", file=sys.stderr)
        raise SystemExit(1) from error

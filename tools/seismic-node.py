#!/usr/bin/python3

"""Operator CLI for Seismic checkpoint installation and node management.

Argument parsing and top-level workflow composition live here; safety-critical
validation and state changes live in the ``seismic_node`` package.  Keeping this
file thin makes the supported command surface easy to audit.
"""

from __future__ import annotations

import argparse
import shlex
import shutil
import sys
import time
from pathlib import Path
from typing import NoReturn

from seismic_node import checkpoint, download, observer, rpc, supervisor, validator


def add_inventory_argument(parser: argparse.ArgumentParser) -> None:
    parser.add_argument("--inventory", type=Path)


def add_checkpoint_source_arguments(parser: argparse.ArgumentParser) -> None:
    """Add the mutually exclusive local/remote checkpoint acquisition options."""
    parser.add_argument("--archive", type=Path)
    parser.add_argument("--manifest", type=Path)
    parser.add_argument("--snapshot-api-url")
    parser.add_argument("--snapshot-bearer-token-file", type=Path)
    parser.add_argument("--checkpoint-epoch", type=int)
    parser.add_argument(
        "--checkpoint-policy",
        choices=("exact", "latest-available", "fail-if-newer", "ask"),
        default="ask",
    )
    parser.add_argument(
        "--summit-rpc-url",
        help="trusted Summit RPC used for network epoch and validator status",
    )
    parser.add_argument("--summit-bearer-token-file", type=Path)
    parser.add_argument("--weak-subjectivity-path", type=Path)
    parser.add_argument("--weak-subjectivity-url")
    parser.add_argument("--weak-subjectivity-rpc-url")
    parser.add_argument("--weak-subjectivity-bearer-token-file", type=Path)
    parser.add_argument(
        "--allow-same-origin-weak-subjectivity",
        action="store_true",
        help=(
            "allow the remote weak-subjectivity source to share the snapshot "
            "provider origin without an interactive confirmation"
        ),
    )
    parser.add_argument("--http-timeout", type=float, default=30.0)
    parser.add_argument("--snapshot-poll-interval", type=float, default=10.0)
    parser.add_argument(
        "--snapshot-wait-timeout",
        type=float,
        default=0.0,
        help="seconds to wait for an epoch and manifest; 0 waits indefinitely",
    )


def add_checkpoint_destination_arguments(parser: argparse.ArgumentParser) -> None:
    parser.add_argument("--checkpoint-path", type=Path)
    parser.add_argument(
        "--installed-weak-subjectivity-path",
        type=Path,
        default=checkpoint.DEFAULT_INSTALLED_WEAK_SUBJECTIVITY_PATH,
    )
    parser.add_argument("--backup-root", type=Path)
    add_yes_argument(parser)


def add_yes_argument(parser: argparse.ArgumentParser) -> None:
    parser.add_argument(
        "--yes",
        action="store_true",
        help=(
            "confirm the destructive operation without an interactive prompt; "
            "unattended installs should also pin --checkpoint-epoch with "
            "--checkpoint-policy exact"
        ),
    )


def add_startup_argument(parser: argparse.ArgumentParser) -> None:
    parser.add_argument("--startup-timeout", type=float, default=30.0)


def parse_args() -> argparse.Namespace:
    """Construct the complete command tree without argparse abbreviations."""
    parser = argparse.ArgumentParser(
        description="Install checkpoints and manage Seismic nodes.",
        allow_abbrev=False,
    )
    commands = parser.add_subparsers(dest="command", required=True)

    checkpoint_parser = commands.add_parser("checkpoint", allow_abbrev=False)
    checkpoint_commands = checkpoint_parser.add_subparsers(
        dest="checkpoint_command", required=True
    )
    checkpoint_install = checkpoint_commands.add_parser("install", allow_abbrev=False)
    checkpoint_install.add_argument(
        "--role", choices=("validator", "observer"), required=True
    )
    add_inventory_argument(checkpoint_install)
    add_checkpoint_source_arguments(checkpoint_install)
    add_checkpoint_destination_arguments(checkpoint_install)

    checkpoint_rollback = checkpoint_commands.add_parser("rollback", allow_abbrev=False)
    checkpoint_rollback.add_argument("--backup", type=Path, required=True)
    add_yes_argument(checkpoint_rollback)

    checkpoint_delete = checkpoint_commands.add_parser(
        "delete-backup", allow_abbrev=False
    )
    checkpoint_delete.add_argument("--backup", type=Path, required=True)
    add_yes_argument(checkpoint_delete)

    validator_parser = commands.add_parser("validator", allow_abbrev=False)
    validator_commands = validator_parser.add_subparsers(
        dest="validator_command", required=True
    )
    validator_deposit = validator_commands.add_parser(
        "deposit-signature", allow_abbrev=False
    )
    add_inventory_argument(validator_deposit)
    validator_deposit.add_argument("--output", type=Path, required=True)
    validator_deposit.add_argument("--overwrite", action="store_true")
    validator_deposit.add_argument("--http-timeout", type=float, default=10.0)
    add_startup_argument(validator_deposit)

    validator_onboard = validator_commands.add_parser(
        "onboard",
        allow_abbrev=False,
        help="install or validate a checkpoint and start validator services",
        formatter_class=argparse.RawDescriptionHelpFormatter,
        epilog=(
            "Manual checkpoint-start equivalent after lifecycle authorization:\n"
            "  sudo systemctl enable --now supervisor\n"
            "  sudo supervisorctl reread\n"
            "  sudo supervisorctl update\n"
            "  sudo supervisorctl start custodian          # when configured\n"
            "  sudo supervisorctl start reth\n"
            "  sudo supervisorctl start summit-checkpoint\n"
            "  sudo supervisorctl start checkpointer       # when configured"
        ),
    )
    add_inventory_argument(validator_onboard)
    validator_onboard.add_argument("--deposit-signature", type=Path, required=True)
    validator_onboard.add_argument(
        "--pre-joining-policy",
        choices=("wait", "start", "leave-stopped"),
    )
    validator_onboard.add_argument("--validator-poll-interval", type=float, default=5.0)
    validator_onboard.add_argument(
        "--validator-wait-timeout",
        type=float,
        default=0.0,
        help="seconds to wait for Joining; 0 waits indefinitely",
    )
    add_checkpoint_source_arguments(validator_onboard)
    add_checkpoint_destination_arguments(validator_onboard)
    add_startup_argument(validator_onboard)

    validator_stop = validator_commands.add_parser(
        "stop",
        allow_abbrev=False,
        help="stop validator services in reverse dependency order",
        formatter_class=argparse.RawDescriptionHelpFormatter,
        epilog=(
            "Manual equivalent:\n"
            "  sudo supervisorctl stop checkpointer       # when configured\n"
            "  sudo supervisorctl stop summit-deposit-rpc\n"
            "  sudo supervisorctl stop summit\n"
            "  sudo supervisorctl stop summit-checkpoint\n"
            "  sudo supervisorctl stop reth\n"
            "  sudo supervisorctl stop custodian          # when configured\n"
            "Supervisor and OpenResty remain running."
        ),
    )
    add_inventory_argument(validator_stop)

    observer_parser = commands.add_parser("observer", allow_abbrev=False)
    observer_commands = observer_parser.add_subparsers(
        dest="observer_command", required=True
    )
    observer_start = observer_commands.add_parser(
        "start",
        allow_abbrev=False,
        help="prepare Supervisor and start observer services",
        formatter_class=argparse.RawDescriptionHelpFormatter,
        epilog=(
            "Manual normal-mode equivalent:\n"
            "  sudo systemctl enable --now supervisor\n"
            "  sudo supervisorctl reread\n"
            "  sudo supervisorctl update\n"
            "  sudo supervisorctl start custodian       # when configured\n"
            "  sudo supervisorctl start reth\n"
            "  sudo supervisorctl start summit-observer\n"
            "  sudo supervisorctl start checkpointer    # when configured\n"
            "For checkpoint mode, start summit-observer-checkpoint instead."
        ),
    )
    observer_start.add_argument(
        "--mode", choices=("normal", "checkpoint"), required=True
    )
    add_inventory_argument(observer_start)
    add_checkpoint_source_arguments(observer_start)
    add_checkpoint_destination_arguments(observer_start)
    add_startup_argument(observer_start)

    observer_stop = observer_commands.add_parser(
        "stop",
        allow_abbrev=False,
        help="stop observer services in reverse dependency order",
        formatter_class=argparse.RawDescriptionHelpFormatter,
        epilog=(
            "Manual equivalent:\n"
            "  sudo supervisorctl stop checkpointer                 # when configured\n"
            "  sudo supervisorctl stop summit-observer\n"
            "  sudo supervisorctl stop summit-observer-checkpoint\n"
            "  sudo supervisorctl stop reth\n"
            "  sudo supervisorctl stop custodian                    # when configured\n"
            "Supervisor and OpenResty remain running."
        ),
    )
    add_inventory_argument(observer_stop)

    return parser.parse_args()


def checkpoint_source_requested(args: argparse.Namespace) -> bool:
    """Return whether local files or a remote snapshot provider were selected."""
    return any(
        value is not None
        for value in (
            args.archive,
            args.manifest,
            args.snapshot_api_url,
        )
    )


def checkpoint_install_options_requested(args: argparse.Namespace) -> bool:
    """Detect install-only modifiers that are meaningless without a source."""
    return (
        any(
            value is not None
            for value in (
                args.snapshot_bearer_token_file,
                args.checkpoint_epoch,
                args.weak_subjectivity_path,
                args.weak_subjectivity_url,
                args.weak_subjectivity_rpc_url,
                args.weak_subjectivity_bearer_token_file,
                args.checkpoint_path,
                args.backup_root,
            )
        )
        or args.yes
        or args.allow_same_origin_weak_subjectivity
        or args.checkpoint_policy != "ask"
        or args.installed_weak_subjectivity_path
        != checkpoint.DEFAULT_INSTALLED_WEAK_SUBJECTIVITY_PATH
    )


def require_checkpoint_source_for_install_options(
    args: argparse.Namespace,
    source_requested: bool,
) -> None:
    """Reject install-only options that would otherwise produce indirect errors."""
    if not source_requested and checkpoint_install_options_requested(args):
        raise checkpoint.CheckpointError(
            "Checkpoint installation options require --snapshot-api-url or local "
            "--archive and --manifest files"
        )


def validate_positive_options(args: argparse.Namespace) -> None:
    for name in (
        "http_timeout",
        "snapshot_poll_interval",
        "startup_timeout",
        "validator_poll_interval",
    ):
        if hasattr(args, name) and getattr(args, name) <= 0:
            raise checkpoint.CheckpointError(
                f"--{name.replace('_', '-')} must be greater than zero"
            )
    for name in ("snapshot_wait_timeout", "validator_wait_timeout"):
        if hasattr(args, name) and getattr(args, name) < 0:
            raise checkpoint.CheckpointError(
                f"--{name.replace('_', '-')} must not be negative"
            )
    if (
        hasattr(args, "checkpoint_epoch")
        and args.checkpoint_epoch is not None
        and args.checkpoint_epoch < 0
    ):
        raise checkpoint.CheckpointError("--checkpoint-epoch must not be negative")


def install_from_resolved_inputs(
    args: argparse.Namespace,
    role: str,
    *,
    network_rpc_url: str | None = None,
    network_bearer_token_file: Path | None = None,
) -> Path:
    """Bridge non-destructive acquisition to the transactional installer."""
    with download.resolve_checkpoint_inputs(
        args,
        role,
        network_rpc_url=network_rpc_url,
        network_bearer_token_file=network_bearer_token_file,
    ) as resolved:
        values = vars(args).copy()
        values.update(
            role=role,
            archive=resolved.archive,
            manifest=resolved.manifest,
            weak_subjectivity_path=resolved.weak_subjectivity,
        )
        return checkpoint.install_checkpoint(argparse.Namespace(**values))


def handle_checkpoint(args: argparse.Namespace) -> None:
    if args.checkpoint_command == "install":
        install_from_resolved_inputs(
            args,
            args.role,
            network_rpc_url=args.summit_rpc_url,
            network_bearer_token_file=args.summit_bearer_token_file,
        )
    elif args.checkpoint_command == "rollback":
        checkpoint.rollback_checkpoint(args)
    else:
        checkpoint.delete_backup(args)


def print_startup_rollback(backup: Path) -> None:
    print(
        "Checkpoint state remains installed after the startup failure.",
        file=sys.stderr,
    )
    print("Rollback command:", file=sys.stderr)
    print(
        f"  sudo {shlex.quote(str(Path(__file__).resolve()))} checkpoint rollback "
        f"--backup {shlex.quote(str(backup))}",
        file=sys.stderr,
    )


def handle_validator(args: argparse.Namespace) -> None:
    """Generate a deposit response, onboard, or stop validator services."""
    if args.validator_command == "stop":
        validator.stop_validator(args)
        return
    if args.validator_command == "deposit-signature":
        validator.generate_deposit_signature(args)
        return
    if args.summit_rpc_url is None:
        raise checkpoint.CheckpointError("validator onboard requires --summit-rpc-url")
    _, node_public_key = validator.load_deposit_response(args.deposit_signature)
    source_requested = checkpoint_source_requested(args)
    require_checkpoint_source_for_install_options(args, source_requested)
    if not source_requested:
        checkpoint.validate_checkpoint_start_configuration("validator")
    # Use one deadline across both lifecycle checks so installation cannot reset
    # the operator's total wait budget.
    wait_deadline = (
        None
        if args.validator_wait_timeout == 0
        else time.monotonic() + args.validator_wait_timeout
    )
    # Decide whether the operator wants to start before downloading or replacing
    # state. If waiting is selected, checkpoint selection happens after Joining
    # and can therefore use the newest completed onboarding checkpoint.
    start_decision = validator.wait_for_start_authorization(
        args,
        node_public_key,
        wait_deadline=wait_deadline,
    )
    backup: Path | None = None
    if source_requested:
        backup = install_from_resolved_inputs(
            args,
            "validator",
            network_rpc_url=args.summit_rpc_url,
            network_bearer_token_file=args.summit_bearer_token_file,
        )
    if not start_decision.start:
        if source_requested:
            print("Checkpoint was installed. All validator services remain stopped.")
        else:
            print(
                "Existing checkpoint remains installed. "
                "All validator services remain stopped."
            )
        return
    try:
        # Recheck status after installation because lifecycle state may have
        # changed while a large checkpoint was downloaded and installed.
        validator.start_checkpoint_validator(
            args,
            node_public_key,
            allow_pre_joining_start=start_decision.pre_joining,
            wait_deadline=wait_deadline,
        )
    except Exception:
        if backup is not None:
            print_startup_rollback(backup)
        raise


def handle_observer(args: argparse.Namespace) -> None:
    """Start or stop observer services, optionally installing a checkpoint."""
    if args.observer_command == "stop":
        observer.stop_observer(args)
        return

    source_requested = checkpoint_source_requested(args)
    require_checkpoint_source_for_install_options(args, source_requested)
    if args.mode == "normal" and source_requested:
        raise checkpoint.CheckpointError(
            "Checkpoint source options cannot be used with observer --mode normal"
        )
    backup: Path | None = None
    if args.mode == "checkpoint" and source_requested:
        backup = install_from_resolved_inputs(
            args,
            "observer",
            network_rpc_url=args.summit_rpc_url,
            network_bearer_token_file=args.summit_bearer_token_file,
        )
    try:
        observer.start_observer(args)
    except Exception:
        if backup is not None:
            print_startup_rollback(backup)
        raise


def main() -> NoReturn:
    """Validate common options, require root, and dispatch one command."""
    args = parse_args()
    validate_positive_options(args)
    checkpoint.require_root()
    if args.command == "checkpoint":
        handle_checkpoint(args)
    elif args.command == "validator":
        handle_validator(args)
    else:
        handle_observer(args)
    raise SystemExit(0)


if __name__ == "__main__":
    try:
        main()
    except KeyboardInterrupt:
        print("seismic-node: interrupted", file=sys.stderr)
        raise SystemExit(130) from None
    except (
        checkpoint.CheckpointError,
        rpc.NetworkError,
        supervisor.SupervisorError,
        OSError,
        shutil.Error,
    ) as error:
        print(f"seismic-node: {error}", file=sys.stderr)
        raise SystemExit(1) from error

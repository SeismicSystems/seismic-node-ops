"""Validator deposit-signature generation, lifecycle-gated startup, and shutdown.

Deposit signing is intentionally isolated behind the temporary loopback-only
Summit deposit RPC. Onboarding derives identity from the installed keys via
Summit's read-only keys command and asks a trusted RPC whether startup is safe.
"""

from __future__ import annotations

import json
import re
import subprocess
import sys
import time
from dataclasses import dataclass
from pathlib import Path
from typing import Any

from . import checkpoint, fsutil, monitoring, rpc, systemd
from .errors import NodeError

DEPOSIT_AMOUNT_GWEI = 32_000_000_000
WITHDRAWAL_ADDRESS = "0xd412c5ecd343e264381ff15afc0ad78a67b79f35"
DEPOSIT_RPC_URL = "http://127.0.0.1:3031"
SUMMIT = Path("/usr/local/bin/summit")
PRE_JOINING_STATUSES = {"NotFound", "Inactive"}
REFUSED_STATUSES = {"SubmittedExitRequest", "FullPayoutPending"}


@dataclass(frozen=True)
class StartDecision:
    """Whether to start and whether the operator authorized an early start."""

    start: bool
    pre_joining: bool = False


def require_byte_array(value: Any, length: int, description: str) -> list[int]:
    """Validate byte arrays emitted by Summit's JSON serialization."""
    if (
        not isinstance(value, list)
        or len(value) != length
        or any(
            isinstance(item, bool) or not isinstance(item, int) or not 0 <= item <= 255
            for item in value
        )
    ):
        raise NodeError(f"{description} must contain exactly {length} bytes")
    return value


def validate_deposit_response(response: dict[str, Any]) -> str:
    """Validate the exact deposit response and return its node public key."""
    if set(response) != {"jsonrpc", "result", "id"}:
        raise NodeError("Deposit RPC returned an invalid JSON-RPC schema")
    if response["jsonrpc"] != "2.0" or response["id"] != 1:
        raise NodeError("Deposit RPC returned an invalid JSON-RPC envelope")
    result = response["result"]
    if not isinstance(result, dict) or set(result) != {
        "node_pubkey",
        "consensus_pubkey",
        "withdrawal_credentials",
        "node_signature",
        "consensus_signature",
        "deposit_data_root",
    }:
        raise NodeError("Deposit RPC returned an invalid result schema")
    node_public_key = require_byte_array(
        result["node_pubkey"], 32, "Deposit response node_pubkey"
    )
    require_byte_array(
        result["consensus_pubkey"], 48, "Deposit response consensus_pubkey"
    )
    withdrawal_credentials = require_byte_array(
        result["withdrawal_credentials"],
        32,
        "Deposit response withdrawal_credentials",
    )
    require_byte_array(result["node_signature"], 64, "Deposit response node_signature")
    require_byte_array(
        result["consensus_signature"], 96, "Deposit response consensus_signature"
    )
    require_byte_array(
        result["deposit_data_root"], 32, "Deposit response deposit_data_root"
    )
    # Execution-address withdrawal credentials use prefix 0x01, eleven zero
    # bytes, and the fixed 20-byte withdrawal address.
    expected_withdrawal = bytes.fromhex(WITHDRAWAL_ADDRESS.removeprefix("0x"))
    if (
        withdrawal_credentials[0] != 0x01
        or bytes(withdrawal_credentials[1:12]) != bytes(11)
        or bytes(withdrawal_credentials[12:]) != expected_withdrawal
    ):
        raise NodeError(
            "Deposit response withdrawal credentials do not match the required address"
        )
    return bytes(node_public_key).hex()


def load_deposit_response(path: Path) -> tuple[dict[str, Any], str]:
    """Read a root-managed deposit response and extract the validator identity."""
    response = fsutil.read_json(path, "Deposit-signature response", root_managed=True)
    return response, validate_deposit_response(response)


def installed_node_public_key(inventory: dict[str, Any]) -> str:
    """Read public identity using Summit's read-only command, never print secrets."""
    keys_dir = inventory["summit_keys_dir"]
    fsutil.require_directory(keys_dir, "Summit keys directory")
    for name in ("node_key.pem", "consensus_key.pem"):
        fsutil.require_regular_file(keys_dir / name, f"Summit {name}")
    try:
        result = subprocess.run(
            [str(SUMMIT), "keys", "show", "--key-store-path", str(keys_dir)],
            stdin=subprocess.DEVNULL,
            stdout=subprocess.PIPE,
            stderr=subprocess.DEVNULL,
            text=True,
            check=True,
            timeout=30.0,
        )
    except (OSError, subprocess.SubprocessError, UnicodeError):
        # Do not surface subprocess output: key-decoding failures could include
        # sensitive input. Only the validated public key may leave this helper.
        raise NodeError(
            f"Could not read installed validator identity with {SUMMIT} keys show; "
            "check the Summit executable and installed key files"
        ) from None
    public_keys = re.findall(
        r"^[ \t]*Node Public Key \(ed25519\):[ \t]*(?:0[xX])?([0-9a-fA-F]{64})[ \t]*$",
        result.stdout,
        re.MULTILINE,
    )
    if len(public_keys) != 1:
        raise NodeError(
            "Summit keys show did not return exactly one valid ed25519 node public key"
        )
    return public_keys[0].lower()


def prompt_operator(message: str) -> str:
    """Read an interactive answer or explain which explicit option is needed."""
    try:
        return input(message)
    except EOFError as error:
        raise NodeError(
            "Interactive input is unavailable; provide an explicit option"
        ) from error


def confirm_output_overwrite(path: Path, overwrite: bool) -> None:
    """Protect an existing signature handoff file from accidental replacement."""
    if not path.exists() and not path.is_symlink():
        return
    fsutil.require_root_managed_file(path, "Existing deposit-signature output")
    if overwrite:
        return
    if not sys.stdin.isatty():
        raise NodeError(
            f"Deposit-signature output already exists; use --overwrite to replace it: {path}"
        )
    response = (
        prompt_operator(f"Replace existing deposit-signature file {path}? [y/N] ")
        .strip()
        .lower()
    )
    if response not in {"y", "yes"}:
        raise NodeError("Deposit-signature output was not replaced")


def generate_deposit_signature(args: Any) -> None:
    """Run the deposit RPC briefly, write one response, and always stop it."""
    fsutil.require_root()
    checkpoint.load_node("validator", checkpoint.node_env_path(args))
    output = args.output
    fsutil.require_absolute(output, "Deposit-signature output")
    confirm_output_overwrite(output, args.overwrite)
    # Pick up freshly installed units; nothing is enabled, so nothing starts.
    systemd.reload_units()
    # Deposit signing uses the same keys and state as Summit, so no other node
    # service may be active during this short-lived RPC operation.
    systemd.require_stopped(
        (
            "custodian",
            "reth",
            "summit",
            "summit-checkpoint",
            "summit-deposit-rpc",
            "checkpointer",
        )
    )

    started: list[str] = []
    operation_error: BaseException | None = None
    try:
        systemd.start_service(
            "summit-deposit-rpc",
            on_start_requested=lambda: started.append("summit-deposit-rpc"),
        )
        deadline = time.monotonic() + args.startup_timeout
        while True:
            try:
                response = rpc.json_rpc_response(
                    DEPOSIT_RPC_URL,
                    "getDepositSignature",
                    [DEPOSIT_AMOUNT_GWEI, WITHDRAWAL_ADDRESS],
                    timeout=min(args.http_timeout, 5.0),
                )
                break
            except rpc.JsonRpcError as error:
                raise NodeError(f"Deposit RPC rejected the request: {error}") from error
            except rpc.NetworkError as error:
                if time.monotonic() >= deadline:
                    raise NodeError(
                        f"Deposit RPC did not become ready: {error}"
                    ) from error
                time.sleep(0.5)
        node_public_key = validate_deposit_response(response)
        checkpoint.atomic_write(
            output,
            json.dumps(response, indent=2).encode() + b"\n",
            0o600,
        )
    except BaseException as error:
        operation_error = error
        raise
    finally:
        # The RPC is never intended to remain available after the handoff file
        # has been produced, including when validation or writing fails.
        if started:
            try:
                systemd.stop_service("summit-deposit-rpc")
            except systemd.ServiceError:
                if operation_error is None:
                    raise
                print(
                    "Warning: failed to stop summit-deposit-rpc after another error.",
                    file=sys.stderr,
                )

    print(f"Deposit-signature response: {output}")
    print(f"Validator node public key: {node_public_key}")
    print("summit-deposit-rpc is stopped.")


def validator_account(
    rpc_url: str,
    node_public_key: str,
    *,
    token: str | None,
    timeout: float,
) -> dict[str, Any] | None:
    """Fetch and validate the account matching a deposit node public key."""
    try:
        result = rpc.json_rpc(
            rpc_url,
            "getValidatorAccount",
            [node_public_key],
            token=token,
            timeout=timeout,
        )
    except rpc.JsonRpcError as error:
        if error.code == 3000:
            return None
        raise NodeError(str(error)) from error
    except rpc.NetworkError as error:
        raise NodeError(str(error)) from error
    expected = {
        "consensus_public_key",
        "withdrawal_credentials",
        "balance",
        "status",
        "joining_epoch",
        "last_deposit_index",
    }
    if not isinstance(result, dict) or set(result) != expected:
        raise NodeError("getValidatorAccount returned an invalid result schema")
    require_byte_array(
        result["consensus_public_key"],
        48,
        "Validator account consensus_public_key",
    )
    withdrawal_credentials = require_byte_array(
        result["withdrawal_credentials"],
        20,
        "Validator account withdrawal_credentials",
    )
    expected_withdrawal = bytes.fromhex(WITHDRAWAL_ADDRESS.removeprefix("0x"))
    if bytes(withdrawal_credentials) != expected_withdrawal:
        raise NodeError(
            "Validator account withdrawal credentials do not match the required address"
        )
    if not isinstance(result["status"], str):
        raise NodeError("Validator account status must be a string")
    for key in ("balance", "joining_epoch", "last_deposit_index"):
        if (
            isinstance(result[key], bool)
            or not isinstance(result[key], int)
            or result[key] < 0
        ):
            raise NodeError(f"Validator account {key} must be a non-negative integer")
    return result


def status_name(account: dict[str, Any] | None) -> str:
    """Map Summit's not-found response to the lifecycle name used by policy."""
    return "NotFound" if account is None else account["status"]


def choose_pre_joining_action(status: str, policy: str | None) -> str:
    """Resolve wait/start/leave-stopped behavior before the Joining state."""
    if policy is not None:
        return policy
    if not sys.stdin.isatty():
        raise NodeError(
            "Validator is not Joining; non-interactive use requires "
            "--pre-joining-policy"
        )
    print(f"Validator account status: {status}")
    if status == "Inactive":
        print(
            "The deposit may still be processing, may be below the minimum stake, "
            "or may not have been accepted as expected."
        )
    print(
        "Starting before Joining may not allow Summit to connect or synchronize yet. "
        "Summit is restarted by systemd on failure, not on a clean exit; rerunning this command may be necessary."
    )
    print("Choose how to proceed:")
    print("  [1] wait for Joining (default)")
    print("  [2] start when preparation completes")
    print("  [3] continue without starting services")
    while True:
        response = prompt_operator("Select [1/2/3], default 1: ").strip().lower()
        if response in {"", "1", "wait", "w"}:
            return "wait"
        if response in {"2", "start", "s"}:
            confirmation = (
                prompt_operator("Start the validator before Joining? [y/N] ")
                .strip()
                .lower()
            )
            if confirmation not in {"y", "yes"}:
                raise NodeError("Pre-Joining validator startup was not confirmed")
            return "start"
        if response in {"3", "leave-stopped", "leave", "l"}:
            return "leave-stopped"
        print("Enter 1, 2, or 3.")


def wait_for_start_authorization(
    args: Any,
    node_public_key: str,
    *,
    allow_pre_joining_start: bool = False,
    wait_deadline: float | None = None,
) -> StartDecision:
    """Poll lifecycle state until startup is authorized or explicitly refused."""
    token = rpc.read_bearer_token(args.summit_bearer_token_file)
    deadline = wait_deadline
    if deadline is None and args.validator_wait_timeout != 0:
        deadline = time.monotonic() + args.validator_wait_timeout
    last_status: str | None = None
    policy = args.pre_joining_policy
    while True:
        account = validator_account(
            args.summit_rpc_url,
            node_public_key,
            token=token,
            timeout=args.http_timeout,
        )
        current_status = status_name(account)
        if current_status != last_status:
            print(f"Validator account status: {current_status}")
            if account is not None:
                print(f"Validator balance: {account['balance']} gwei")
                print(f"Validator joining epoch: {account['joining_epoch']}")
            last_status = current_status

        if current_status == "Joining":
            return StartDecision(start=True)
        if current_status == "Active":
            print(
                "Warning: validator is already Active and missed the preferred "
                "Joining synchronization window.",
                file=sys.stderr,
            )
            return StartDecision(start=True)
        if current_status in REFUSED_STATUSES:
            raise NodeError(
                f"Refusing to start validator in lifecycle state {current_status}"
            )
        if current_status not in PRE_JOINING_STATUSES:
            raise NodeError(
                f"Refusing unknown validator lifecycle state {current_status!r}"
            )

        # A second status check occurs before startup in either mode. Preserve
        # a previously confirmed early-start choice instead of prompting twice.
        if allow_pre_joining_start:
            action = "start"
        else:
            action = choose_pre_joining_action(current_status, policy)
        if action == "start":
            print(
                f"Warning: starting validator while status is {current_status}.",
                file=sys.stderr,
            )
            return StartDecision(start=True, pre_joining=True)
        if action == "leave-stopped":
            return StartDecision(start=False)
        policy = "wait"
        if deadline is not None and time.monotonic() >= deadline:
            raise NodeError(
                "Timed out waiting for validator status Joining; services remain stopped"
            )
        print(
            "Waiting for validator status Joining "
            f"(current: {current_status}); "
            f"checking again in {args.validator_poll_interval:g}s...",
            flush=True,
        )
        time.sleep(args.validator_poll_interval)


def start_onboarded_validator(
    args: Any,
    node_public_key: str,
    *,
    allow_pre_joining_start: bool,
    wait_deadline: float | None = None,
) -> None:
    """Recheck lifecycle state, validate installed inputs, and start the validator."""
    decision = wait_for_start_authorization(
        args,
        node_public_key,
        allow_pre_joining_start=allow_pre_joining_start,
        wait_deadline=wait_deadline,
    )
    if not decision.start:
        if args.mode == "checkpoint":
            print(
                "Checkpoint remains installed. All validator services remain stopped."
            )
        else:
            print("All validator services remain stopped. No checkpoint was installed.")
        return
    start_validator(args, expected_node_public_key=node_public_key)


def start_validator(args: Any, *, expected_node_public_key: str | None = None) -> None:
    """Start validator services without lifecycle checks or checkpoint downloads.

    ``validator onboard`` remains the lifecycle-gated entry point; this command
    restarts an already onboarded validator from its persisted normal state or
    from the installed checkpoint inputs.
    """
    inventory = checkpoint.load_node("validator", checkpoint.node_env_path(args))
    if (
        expected_node_public_key is not None
        and installed_node_public_key(inventory) != expected_node_public_key
    ):
        raise NodeError(
            "Installed validator node public key changed during onboarding; "
            "refusing to start"
        )
    if args.mode == "checkpoint":
        checkpoint.validate_checkpoint_start_configuration("validator")
        summit_program = "summit-checkpoint"
        conflicting_program = "summit"
    else:
        # The normal unit does not read checkpoint-start config. Mutual
        # exclusion is enforced against the checkpoint unit instead.
        summit_program = "summit"
        conflicting_program = "summit-checkpoint"
    systemd.reload_units()
    systemd.start_node(
        summit_program,
        conflicting_program,
        checkpointer=inventory["env"].flag("CHECKPOINTER_ENABLED"),
    )
    monitoring.ensure_running(inventory, "validator")
    print(f"Validator {args.mode} startup requested successfully.")


def stop_validator(args: Any) -> None:
    """Validate validator identity and stop its services in dependency order."""
    checkpoint.load_node("validator", checkpoint.node_env_path(args))
    systemd.stop_node(("summit-deposit-rpc", "summit", "summit-checkpoint"))
    print("Validator services stopped successfully.")
    print("OpenResty and any running Prometheus Agent remain running.")

"""Resolve local or remote checkpoint inputs before state installation.

This module intentionally keeps network acquisition separate from the destructive
filesystem transaction in :mod:`checkpoint`.  It selects an epoch, downloads and
validates the provider manifest/archive, obtains a weak-subjectivity anchor,
warns when both remote sources share an origin, and yields temporary paths that
exist only for the caller's context.
"""

from __future__ import annotations

import json
import os
import shutil
import sys
import tempfile
import time
from collections.abc import Iterator
from contextlib import contextmanager
from dataclasses import dataclass
from pathlib import Path
from typing import Any

from . import checkpoint, rpc


@dataclass(frozen=True)
class ResolvedCheckpointInputs:
    """Verified input paths and epoch handed to the checkpoint installer."""

    archive: Path
    manifest: Path
    weak_subjectivity: Path
    epoch: int


def snapshot_urls(base_url: str, epoch: int) -> tuple[str, str]:
    """Build the checkpoint-app manifest and archive endpoints for an epoch."""
    parsed = rpc.validate_url(base_url, "Snapshot API URL")
    if parsed.query:
        raise checkpoint.CheckpointError(
            "Snapshot API URL must not contain a query string"
        )
    base = base_url.rstrip("/")
    return (
        f"{base}/checkpoints/{epoch}/manifest",
        f"{base}/checkpoints/{epoch}/snapshot",
    )


def require_nonnegative_integer(value: Any, description: str) -> int:
    """Validate integer epoch values returned by JSON-RPC."""
    if isinstance(value, bool) or not isinstance(value, int) or value < 0:
        raise checkpoint.CheckpointError(
            f"{description} must be a non-negative integer"
        )
    return value


def current_epoch(rpc_url: str, token: str | None, timeout: float) -> int:
    """Read Summit's current epoch from a trusted network RPC."""
    value = rpc.json_rpc(rpc_url, "getLatestEpoch", [], token=token, timeout=timeout)
    return require_nonnegative_integer(value, "Summit current epoch")


def wait_until_epoch_completed(
    selected_epoch: int,
    rpc_url: str,
    token: str | None,
    *,
    interval: float,
    deadline: float | None,
    timeout: float,
) -> int:
    """Poll Summit until ``selected_epoch`` is no longer the live epoch."""
    while True:
        network_epoch = current_epoch(rpc_url, token, timeout)
        if network_epoch > selected_epoch:
            return network_epoch
        print(
            f"Checkpoint epoch {selected_epoch} is not complete yet "
            f"(network epoch {network_epoch}); waiting..."
        )
        sleep_until_retry(
            interval, deadline, "waiting for the checkpoint epoch to complete"
        )


def fetch_manifest_when_available(
    url: str,
    destination: Path,
    token: str | None,
    *,
    interval: float,
    deadline: float | None,
    timeout: float,
    unavailable_hint: str | None = None,
) -> dict[str, Any]:
    """Poll a manifest endpoint; only a 404 is considered a retryable state."""
    hint_pending = unavailable_hint
    while True:
        manifest = try_fetch_manifest(url, destination, token, timeout)
        if manifest is not None:
            return manifest
        print(f"Snapshot manifest is not available yet: {url}; waiting...")
        if hint_pending is not None:
            print(hint_pending)
            hint_pending = None
        sleep_until_retry(interval, deadline, "waiting for the snapshot manifest")


def try_fetch_manifest(
    url: str, destination: Path, token: str | None, timeout: float
) -> dict[str, Any] | None:
    """Fetch one manifest, returning ``None`` only when it is not published."""
    try:
        data = rpc.request_bytes(
            url,
            token=token,
            timeout=timeout,
            maximum_size=rpc.MAX_JSON_BYTES,
            description="Snapshot manifest",
        )
    except rpc.HttpStatusError as error:
        if error.status == 404:
            return None
        raise checkpoint.CheckpointError(str(error)) from error
    except rpc.NetworkError as error:
        raise checkpoint.CheckpointError(str(error)) from error
    destination.write_bytes(data)
    os.chmod(destination, 0o600)
    return checkpoint.load_manifest(destination)


def sleep_until_retry(interval: float, deadline: float | None, activity: str) -> None:
    """Sleep for a polling interval without crossing a configured deadline."""
    if deadline is not None and time.monotonic() + interval > deadline:
        raise checkpoint.CheckpointError(f"Timed out while {activity}")
    time.sleep(interval)


def select_epoch(
    args: Any,
    work_dir: Path,
    network_rpc_url: str,
    network_token: str | None,
    snapshot_token: str | None,
) -> tuple[int, dict[str, Any], Path]:
    """Select a completed checkpoint without silently changing an explicit epoch."""
    deadline = (
        None
        if args.snapshot_wait_timeout == 0
        else time.monotonic() + args.snapshot_wait_timeout
    )
    requested = args.checkpoint_epoch
    if requested is None:
        network_epoch = current_epoch(network_rpc_url, network_token, args.http_timeout)
        if network_epoch == 0:
            raise checkpoint.CheckpointError(
                "The network is still in epoch 0; no completed checkpoint is available"
            )
        selected = network_epoch - 1
    else:
        selected = requested
        network_epoch = wait_until_epoch_completed(
            selected,
            network_rpc_url,
            network_token,
            interval=args.snapshot_poll_interval,
            deadline=deadline,
            timeout=args.http_timeout,
        )

    manifest: dict[str, Any] | None = None
    manifest_path: Path | None = None
    # A manifest for the latest completed epoch is probed once so an operator can
    # choose it without changing the meaning of an explicit epoch silently.
    newest_completed = network_epoch - 1 if network_epoch > 0 else 0
    if requested is not None and newest_completed > selected:
        newer_path = work_dir / f"manifest-{newest_completed}.json"
        newer_url, _ = snapshot_urls(args.snapshot_api_url, newest_completed)
        newer_manifest = try_fetch_manifest(
            newer_url, newer_path, snapshot_token, args.http_timeout
        )
        if newer_manifest is not None:
            if newer_manifest["epoch"] != newest_completed:
                raise checkpoint.CheckpointError(
                    "Newer snapshot manifest does not match its requested epoch"
                )
            selected = choose_newer_checkpoint(
                selected,
                newest_completed,
                args.checkpoint_policy,
            )
            if selected == newest_completed:
                manifest = newer_manifest
                manifest_path = newer_path

    if manifest is None:
        manifest_path = work_dir / f"manifest-{selected}.json"
        manifest_url, _ = snapshot_urls(args.snapshot_api_url, selected)
        # The newest completed epoch may still be processing at the provider.
        # Tell the operator how to use the previous snapshot instead of waiting.
        unavailable_hint = None
        if requested is None and selected > 0:
            unavailable_hint = (
                f"The provider may still be processing epoch {selected}. To use "
                f"the previous snapshot instead, rerun this command with "
                f"--checkpoint-epoch {selected - 1}."
            )
        manifest = fetch_manifest_when_available(
            manifest_url,
            manifest_path,
            snapshot_token,
            interval=args.snapshot_poll_interval,
            deadline=deadline,
            timeout=args.http_timeout,
            unavailable_hint=unavailable_hint,
        )
        if manifest["epoch"] != selected:
            raise checkpoint.CheckpointError(
                f"Snapshot manifest epoch mismatch: requested {selected}, "
                f"received {manifest['epoch']}"
            )
    assert manifest_path is not None
    print(f"Current network epoch: {network_epoch}")
    if requested is not None:
        print(f"Requested checkpoint epoch: {requested}")
    print(f"Selected checkpoint epoch: {selected}")
    return selected, manifest, manifest_path


def prompt_operator(message: str) -> str:
    """Read an interactive policy choice with a useful non-interactive error."""
    try:
        return input(message)
    except EOFError as error:
        raise checkpoint.CheckpointError(
            "Interactive input is unavailable; provide an explicit policy"
        ) from error


def choose_newer_checkpoint(requested: int, newer: int, policy: str) -> int:
    """Apply the explicit automation policy or ask an interactive operator."""
    if policy == "exact":
        print(
            f"Checkpoint {newer} is available; keeping explicitly requested "
            f"checkpoint {requested}."
        )
        return requested
    if policy == "latest-available":
        print(f"Using newer available checkpoint {newer} instead of {requested}.")
        return newer
    if policy == "fail-if-newer":
        raise checkpoint.CheckpointError(
            f"Checkpoint {newer} is available, but checkpoint {requested} was requested"
        )
    if not sys.stdin.isatty():
        raise checkpoint.CheckpointError(
            "A newer checkpoint is available; non-interactive use requires an explicit "
            "--checkpoint-policy"
        )
    print(
        f"Checkpoint {newer} is available and is newer than requested checkpoint {requested}."
    )
    while True:
        response = (
            prompt_operator(
                f"Use [{newer}] newer, [{requested}] requested, or [a]bort? "
            )
            .strip()
            .lower()
        )
        if response in {str(newer), "newer", "n"}:
            return newer
        if response in {str(requested), "requested", "r"}:
            return requested
        if response in {"a", "abort"}:
            raise checkpoint.CheckpointError("Checkpoint selection was aborted")
        print("Enter the newer epoch, requested epoch, or 'abort'.")


def weak_subjectivity_from_rpc(
    url: str,
    epoch: int,
    destination: Path,
    token: str | None,
    timeout: float,
) -> None:
    """Fetch a finalized header digest and serialize the installer's TOML format."""
    try:
        result = rpc.json_rpc(
            url,
            "getFinalizedHeaderDigest",
            [epoch],
            token=token,
            timeout=timeout,
        )
    except rpc.NetworkError as error:
        raise checkpoint.CheckpointError(str(error)) from error
    if not isinstance(result, dict) or set(result) != {"epoch", "digest"}:
        raise checkpoint.CheckpointError(
            "getFinalizedHeaderDigest returned an invalid result schema"
        )
    if result["epoch"] != epoch:
        raise checkpoint.CheckpointError(
            f"Weak-subjectivity RPC returned epoch {result['epoch']!r}, expected {epoch}"
        )
    digest = result["digest"]
    if (
        not isinstance(digest, list)
        or len(digest) != 32
        or any(
            isinstance(value, bool)
            or not isinstance(value, int)
            or not 0 <= value <= 255
            for value in digest
        )
    ):
        raise checkpoint.CheckpointError(
            "Weak-subjectivity RPC returned an invalid 32-byte digest"
        )
    digest_hex = "0x" + bytes(digest).hex()
    destination.write_text(
        f"epoch = {epoch}\nheader_digest = {json.dumps(digest_hex)}\n",
        encoding="utf-8",
    )
    os.chmod(destination, 0o600)


def confirm_same_origin_weak_subjectivity(
    args: Any,
    snapshot_url: str,
    remote_url: str,
) -> None:
    """Warn and require authorization when both trust inputs share an origin."""
    snapshot_origin = rpc.url_origin(snapshot_url)
    remote_origin = rpc.url_origin(remote_url)
    if snapshot_origin != remote_origin:
        return

    scheme, host, port = snapshot_origin
    origin = f"{scheme}://{host}:{port}"
    print(
        "Warning: the snapshot and weak-subjectivity sources share the same "
        f"origin ({origin}).",
        file=sys.stderr,
    )
    print(
        "A compromise of that origin could provide both the checkpoint and the "
        "trust anchor, so the anchor is not independently sourced.",
        file=sys.stderr,
    )
    if getattr(args, "allow_same_origin_weak_subjectivity", False):
        print(
            "Continuing because --allow-same-origin-weak-subjectivity was provided.",
            file=sys.stderr,
        )
        return
    if not sys.stdin.isatty():
        raise checkpoint.CheckpointError(
            "Same-origin weak subjectivity requires "
            "--allow-same-origin-weak-subjectivity in non-interactive use"
        )
    response = (
        prompt_operator("Continue with same-origin weak subjectivity? [y/N] ")
        .strip()
        .lower()
    )
    if response not in {"y", "yes"}:
        raise checkpoint.CheckpointError(
            "Same-origin weak-subjectivity use was not confirmed"
        )


def resolve_weak_subjectivity(
    args: Any,
    epoch: int,
    work_dir: Path,
    snapshot_url: str | None,
) -> Path:
    """Resolve exactly one local, URL, or Summit-RPC trust-anchor source."""
    sources = sum(
        value is not None
        for value in (
            args.weak_subjectivity_path,
            args.weak_subjectivity_url,
            args.weak_subjectivity_rpc_url,
        )
    )
    if sources != 1:
        raise checkpoint.CheckpointError(
            "Specify exactly one weak-subjectivity source: --weak-subjectivity-path, "
            "--weak-subjectivity-url, or --weak-subjectivity-rpc-url"
        )
    if args.weak_subjectivity_path is not None:
        path = args.weak_subjectivity_path
        checkpoint.load_weak_subjectivity(path, epoch)
        return path

    remote_url = args.weak_subjectivity_url or args.weak_subjectivity_rpc_url
    assert remote_url is not None
    # Independent origins are preferred. Same-origin operation remains possible
    # only after the operator accepts the weaker trust model explicitly.
    if snapshot_url is not None:
        confirm_same_origin_weak_subjectivity(args, snapshot_url, remote_url)
    token = rpc.read_bearer_token(args.weak_subjectivity_bearer_token_file)
    destination = work_dir / "weak-subjectivity.toml"
    if args.weak_subjectivity_url is not None:
        try:
            data = rpc.request_bytes(
                args.weak_subjectivity_url,
                token=token,
                timeout=args.http_timeout,
                maximum_size=rpc.MAX_ANCHOR_BYTES,
                description="Weak-subjectivity URL",
            )
        except rpc.NetworkError as error:
            raise checkpoint.CheckpointError(str(error)) from error
        destination.write_bytes(data)
        os.chmod(destination, 0o600)
    else:
        weak_subjectivity_from_rpc(
            args.weak_subjectivity_rpc_url,
            epoch,
            destination,
            token,
            args.http_timeout,
        )
    checkpoint.load_weak_subjectivity(destination, epoch)
    return destination


def create_download_work_dir(args: Any, role: str) -> Path:
    """Create a root-only temporary directory on the node-state filesystem."""
    inventory_path = args.inventory or checkpoint.DEFAULT_INVENTORY_PATHS[role]
    inventory = checkpoint.load_inventory(role, inventory_path)
    download_root = inventory["reth_data_dir"].parent / ".seismic-node-downloads"
    checkpoint.ensure_root_parent(download_root / "placeholder", mode=0o700)
    download_root.mkdir(mode=0o700, exist_ok=True)
    os.chmod(download_root, 0o700)
    return Path(tempfile.mkdtemp(prefix="checkpoint.", dir=download_root))


@contextmanager
def resolve_checkpoint_inputs(
    args: Any,
    role: str,
    *,
    network_rpc_url: str | None = None,
    network_bearer_token_file: Path | None = None,
) -> Iterator[ResolvedCheckpointInputs]:
    """Yield validated local paths and always remove generated downloads afterward."""
    local_source = args.archive is not None or args.manifest is not None
    remote_source = args.snapshot_api_url is not None
    weak_sources = sum(
        value is not None
        for value in (
            args.weak_subjectivity_path,
            args.weak_subjectivity_url,
            args.weak_subjectivity_rpc_url,
        )
    )
    if weak_sources != 1:
        raise checkpoint.CheckpointError(
            "Specify exactly one weak-subjectivity source: --weak-subjectivity-path, "
            "--weak-subjectivity-url, or --weak-subjectivity-rpc-url"
        )
    if local_source == remote_source:
        raise checkpoint.CheckpointError(
            "Specify either local --archive and --manifest files or --snapshot-api-url"
        )
    if local_source:
        if args.archive is None or args.manifest is None:
            raise checkpoint.CheckpointError(
                "Local checkpoint input requires both --archive and --manifest"
            )
        manifest = checkpoint.load_manifest(args.manifest)
        epoch = manifest["epoch"]
        if args.checkpoint_epoch is not None and args.checkpoint_epoch != epoch:
            raise checkpoint.CheckpointError(
                f"--checkpoint-epoch {args.checkpoint_epoch} does not match manifest epoch {epoch}"
            )
        if args.weak_subjectivity_path is not None:
            weak_path = resolve_weak_subjectivity(
                args, epoch, args.manifest.parent, None
            )
            yield ResolvedCheckpointInputs(
                args.archive, args.manifest, weak_path, epoch
            )
            return
        work_dir = create_download_work_dir(args, role)
        try:
            weak_path = resolve_weak_subjectivity(args, epoch, work_dir, None)
            yield ResolvedCheckpointInputs(
                args.archive, args.manifest, weak_path, epoch
            )
        finally:
            shutil.rmtree(work_dir, ignore_errors=True)
        return

    work_dir = create_download_work_dir(args, role)
    try:
        # Snapshot authentication and network-RPC authentication are separate so
        # credentials are never reused implicitly across trust domains.
        snapshot_token = rpc.read_bearer_token(args.snapshot_bearer_token_file)
        effective_network_rpc = network_rpc_url or args.weak_subjectivity_rpc_url
        if effective_network_rpc is None:
            raise checkpoint.CheckpointError(
                "Remote checkpoint selection requires a Summit RPC URL"
            )
        effective_network_token_file = (
            network_bearer_token_file
            if network_rpc_url is not None
            else args.weak_subjectivity_bearer_token_file
        )
        network_token = rpc.read_bearer_token(effective_network_token_file)
        selected, manifest, manifest_path = select_epoch(
            args,
            work_dir,
            effective_network_rpc,
            network_token,
            snapshot_token,
        )
        weak_path = resolve_weak_subjectivity(
            args,
            selected,
            work_dir,
            args.snapshot_api_url,
        )
        _, archive_url = snapshot_urls(args.snapshot_api_url, selected)
        print(f"Snapshot source: {args.snapshot_api_url}")
        weak_source = (
            args.weak_subjectivity_url
            or args.weak_subjectivity_rpc_url
            or str(args.weak_subjectivity_path)
        )
        print(f"Weak-subjectivity source: {weak_source}")
        archive_size = manifest["archive"]["size_bytes"]
        print(
            f"Downloading checkpoint archive for epoch {selected} "
            f"({rpc.human_size(archive_size)})..."
        )
        archive_path = work_dir / f"epoch_{selected}.tar.gz"
        try:
            rpc.download_verified_archive(
                archive_url,
                archive_path,
                token=snapshot_token,
                timeout=args.http_timeout,
                expected_size=manifest["archive"]["size_bytes"],
                expected_sha256=manifest["archive"]["sha256"],
            )
        except rpc.NetworkError as error:
            raise checkpoint.CheckpointError(str(error)) from error
        yield ResolvedCheckpointInputs(
            archive_path,
            manifest_path,
            weak_path,
            selected,
        )
    finally:
        shutil.rmtree(work_dir, ignore_errors=True)

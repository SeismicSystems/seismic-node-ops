"""Strict HTTP and JSON-RPC primitives used by node onboarding.

The standard library is used deliberately so the operator CLI has no runtime
Python dependencies.  Every request is bounded, redirects are disabled, remote
plain HTTP is rejected, and bearer tokens are loaded only from protected files.
"""

from __future__ import annotations

import hashlib
import ipaddress
import json
import os
import shutil
import stat
import sys
import time
import urllib.error
import urllib.parse
import urllib.request
from pathlib import Path
from typing import Any

MAX_JSON_BYTES = 1024 * 1024
MAX_ANCHOR_BYTES = 16 * 1024
PROGRESS_REFRESH_SECONDS = 0.5


class NetworkError(Exception):
    """A download or JSON-RPC request failed validation."""


class HttpStatusError(NetworkError):
    """An HTTP endpoint returned a non-success status."""

    def __init__(self, status: int, url: str) -> None:
        super().__init__(f"HTTP {status} from {url}")
        self.status = status
        self.url = url


class JsonRpcError(NetworkError):
    """A remote JSON-RPC endpoint returned an error object."""

    def __init__(self, code: int, message: str) -> None:
        super().__init__(f"JSON-RPC error {code}: {message}")
        self.code = code
        self.message = message


def human_size(value: int) -> str:
    """Format a byte count for operator-facing progress output."""
    units = ("B", "KiB", "MiB", "GiB", "TiB")
    amount = float(value)
    for unit in units:
        if amount < 1024 or unit == units[-1]:
            return f"{amount:.1f} {unit}"
        amount /= 1024
    return f"{value} B"


class DownloadProgress:
    """Render download progress without external dependencies.

    On a terminal the same line is redrawn with percentage, size, and rate.
    Without a terminal (logs, CI) one line is printed per ten-percent step.
    """

    def __init__(self, total: int) -> None:
        self.total = total
        self.started_at = time.monotonic()
        self.last_render = 0.0
        self.interactive = sys.stdout.isatty()
        self.next_milestone = 10
        self.line_open = False

    def render_line(self, written: int) -> str:
        percent = 100 * written // self.total if self.total else 100
        elapsed = time.monotonic() - self.started_at
        rate = written / elapsed if elapsed > 0 else 0.0
        return (
            f"  {percent:3d}%  {human_size(written)} / {human_size(self.total)}"
            f"  ({human_size(int(rate))}/s)"
        )

    def update(self, written: int) -> None:
        if self.interactive:
            now = time.monotonic()
            if (
                now - self.last_render < PROGRESS_REFRESH_SECONDS
                and written < self.total
            ):
                return
            self.last_render = now
            print(f"\r{self.render_line(written):<70}", end="", flush=True)
            self.line_open = True
            return
        percent = 100 * written // self.total if self.total else 100
        if percent >= self.next_milestone:
            print(f" {self.render_line(written)}")
            self.next_milestone = percent + 10

    def finish(self) -> None:
        """Terminate an in-place progress line so later output starts cleanly."""
        if self.interactive and self.line_open:
            print(flush=True)
            self.line_open = False


class NoRedirectHandler(urllib.request.HTTPRedirectHandler):
    """Prevent credentials or trust decisions from crossing redirects."""

    def redirect_request(
        self, req: Any, fp: Any, code: int, msg: str, headers: Any, newurl: str
    ) -> None:
        return None


def validate_url(url: str, description: str) -> urllib.parse.SplitResult:
    """Validate transport and credential rules before constructing a request."""
    try:
        parsed = urllib.parse.urlsplit(url)
    except ValueError as error:
        raise NetworkError(f"Invalid {description}: {error}") from error
    if parsed.scheme not in {"http", "https"} or not parsed.hostname:
        raise NetworkError(f"{description} must be an HTTP or HTTPS URL: {url}")
    try:
        _ = parsed.port
    except ValueError as error:
        raise NetworkError(f"Invalid {description}: {error}") from error
    if parsed.username is not None or parsed.password is not None:
        raise NetworkError(f"{description} must not contain credentials: {url}")
    if parsed.fragment:
        raise NetworkError(f"{description} must not contain a fragment: {url}")
    if parsed.scheme == "http" and not is_loopback_host(parsed.hostname):
        raise NetworkError(
            f"{description} must use HTTPS for a non-loopback host: {url}"
        )
    return parsed


def is_loopback_host(host: str) -> bool:
    """Return whether a host is an explicit loopback name or address."""
    if host.lower() == "localhost":
        return True
    try:
        return ipaddress.ip_address(host).is_loopback
    except ValueError:
        return False


def url_origin(url: str) -> tuple[str, str, int]:
    """Normalize an origin for snapshot/anchor independence comparisons."""
    parsed = validate_url(url, "URL")
    default_port = 443 if parsed.scheme == "https" else 80
    return parsed.scheme, parsed.hostname.lower(), parsed.port or default_port


def read_bearer_token(path: Path | None) -> str | None:
    """Read one token from a root-owned, non-symlinked, private file."""
    if path is None:
        return None
    if not path.is_absolute() or path == Path("/"):
        raise NetworkError(
            f"Bearer-token file must be an absolute non-root path: {path}"
        )
    try:
        metadata = path.lstat()
    except OSError as error:
        raise NetworkError(
            f"Could not inspect bearer-token file {path}: {error}"
        ) from error
    if stat.S_ISLNK(metadata.st_mode) or not stat.S_ISREG(metadata.st_mode):
        raise NetworkError(f"Bearer-token file is not a safe regular file: {path}")
    if metadata.st_uid != 0 or metadata.st_mode & 0o077:
        raise NetworkError(
            f"Bearer-token file must be root-owned with mode 0600 or stricter: {path}"
        )
    # Re-open with O_NOFOLLOW and re-check the descriptor to narrow the race
    # between pathname validation and reading sensitive token contents.
    try:
        descriptor = os.open(path, os.O_RDONLY | os.O_NOFOLLOW)
        opened_metadata = os.fstat(descriptor)
        if (
            not stat.S_ISREG(opened_metadata.st_mode)
            or opened_metadata.st_uid != 0
            or opened_metadata.st_mode & 0o077
        ):
            os.close(descriptor)
            raise NetworkError(f"Bearer-token file changed during validation: {path}")
        with os.fdopen(descriptor, encoding="utf-8") as file:
            lines = file.read().splitlines()
    except OSError as error:
        raise NetworkError(
            f"Could not read bearer-token file {path}: {error}"
        ) from error
    if len(lines) != 1 or not lines[0].strip():
        raise NetworkError(
            f"Bearer-token file must contain exactly one non-empty line: {path}"
        )
    return lines[0].strip()


def build_request(
    url: str, *, data: bytes | None, token: str | None, content_type: str | None
) -> urllib.request.Request:
    """Construct a GET or POST request with optional bearer authentication."""
    headers = {"User-Agent": "seismic-node-ops/1"}
    if token is not None:
        headers["Authorization"] = f"Bearer {token}"
    if content_type is not None:
        headers["Content-Type"] = content_type
    return urllib.request.Request(
        url, data=data, headers=headers, method="POST" if data is not None else "GET"
    )


def open_request(request: urllib.request.Request, timeout: float) -> Any:
    """Open a request with redirects disabled and normalized network errors."""
    opener = urllib.request.build_opener(NoRedirectHandler())
    try:
        return opener.open(request, timeout=timeout)
    except urllib.error.HTTPError as error:
        error.close()
        if 300 <= error.code < 400:
            raise NetworkError(
                f"Redirects are not allowed for {request.full_url}: HTTP {error.code}"
            ) from error
        raise HttpStatusError(error.code, request.full_url) from error
    except urllib.error.URLError as error:
        raise NetworkError(
            f"Could not reach {request.full_url}: {error.reason}"
        ) from error


def request_bytes(
    url: str,
    *,
    token: str | None,
    timeout: float,
    maximum_size: int,
    description: str,
) -> bytes:
    """Download a small response while enforcing a hard byte limit."""
    validate_url(url, description)
    request = build_request(url, data=None, token=token, content_type=None)
    with open_request(request, timeout) as response:
        length = response.headers.get("Content-Length")
        if length is not None:
            try:
                declared_length = int(length)
            except ValueError as error:
                raise NetworkError(
                    f"Invalid Content-Length for {description}"
                ) from error
            if declared_length > maximum_size:
                raise NetworkError(
                    f"{description} exceeds the {maximum_size}-byte limit"
                )
        data = response.read(maximum_size + 1)
    if len(data) > maximum_size:
        raise NetworkError(f"{description} exceeds the {maximum_size}-byte limit")
    return data


def download_verified_archive(
    url: str,
    destination: Path,
    *,
    token: str | None,
    timeout: float,
    expected_size: int,
    expected_sha256: str,
) -> None:
    """Stream an archive to a new file and verify manifest size and SHA-256."""
    validate_url(url, "Snapshot archive URL")
    free_space = shutil.disk_usage(destination.parent).free
    if free_space < expected_size:
        raise NetworkError(
            f"Insufficient space for snapshot download: need {expected_size} bytes, "
            f"have {free_space} bytes"
        )
    request = build_request(url, data=None, token=token, content_type=None)
    digest = hashlib.sha256()
    written = 0
    destination_created = False
    descriptor: int | None = None
    progress = DownloadProgress(expected_size)
    try:
        # O_EXCL and O_NOFOLLOW prevent replacement or symlink tricks at the
        # root-managed destination while the untrusted response is streamed.
        descriptor = os.open(
            destination, os.O_CREAT | os.O_EXCL | os.O_WRONLY | os.O_NOFOLLOW, 0o600
        )
        destination_created = True
        output = os.fdopen(descriptor, "wb")
        descriptor = None  # The file object now owns and closes the descriptor.
        with output, open_request(request, timeout) as response:
            length = response.headers.get("Content-Length")
            if length is not None:
                try:
                    declared_length = int(length)
                except ValueError as error:
                    raise NetworkError(
                        "Snapshot response has an invalid Content-Length"
                    ) from error
                if declared_length != expected_size:
                    raise NetworkError(
                        f"Snapshot Content-Length mismatch: expected {expected_size}, "
                        f"found {declared_length}"
                    )
            while chunk := response.read(1024 * 1024):
                written += len(chunk)
                if written > expected_size:
                    raise NetworkError("Snapshot download exceeded the manifest size")
                digest.update(chunk)
                output.write(chunk)
                progress.update(written)
            output.flush()
            os.fsync(output.fileno())
    except Exception:
        progress.finish()
        if descriptor is not None:
            os.close(descriptor)
        if destination_created:
            destination.unlink(missing_ok=True)
        raise
    progress.finish()
    actual_sha256 = f"0x{digest.hexdigest()}"
    if written != expected_size:
        destination.unlink(missing_ok=True)
        raise NetworkError(
            f"Snapshot size mismatch: expected {expected_size}, found {written}"
        )
    if actual_sha256 != expected_sha256:
        destination.unlink(missing_ok=True)
        raise NetworkError(
            f"Snapshot SHA-256 mismatch: expected {expected_sha256}, found {actual_sha256}"
        )


def json_rpc_response(
    url: str,
    method: str,
    params: list[Any],
    *,
    token: str | None = None,
    timeout: float = 15.0,
) -> dict[str, Any]:
    """Perform one strict JSON-RPC request and return its validated envelope."""
    validate_url(url, "Summit RPC URL")
    request_id = 1
    request_body = json.dumps(
        {"jsonrpc": "2.0", "method": method, "params": params, "id": request_id},
        separators=(",", ":"),
    ).encode()
    request = build_request(
        url, data=request_body, token=token, content_type="application/json"
    )
    with open_request(request, timeout) as response:
        body = response.read(MAX_JSON_BYTES + 1)
    if len(body) > MAX_JSON_BYTES:
        raise NetworkError("JSON-RPC response exceeds the size limit")
    try:
        value = json.loads(body)
    except json.JSONDecodeError as error:
        raise NetworkError(f"Summit RPC returned invalid JSON: {error}") from error
    if (
        not isinstance(value, dict)
        or value.get("jsonrpc") != "2.0"
        or value.get("id") != request_id
    ):
        raise NetworkError("Summit RPC returned an invalid JSON-RPC envelope")
    if "error" in value:
        error = value["error"]
        if (
            not isinstance(error, dict)
            or not isinstance(error.get("code"), int)
            or not isinstance(error.get("message"), str)
        ):
            raise NetworkError("Summit RPC returned a malformed error object")
        raise JsonRpcError(error["code"], error["message"])
    if "result" not in value:
        raise NetworkError("Summit RPC response has no result")
    return value


def json_rpc(
    url: str,
    method: str,
    params: list[Any],
    *,
    token: str | None = None,
    timeout: float = 15.0,
) -> Any:
    """Return only the result member from :func:`json_rpc_response`."""
    return json_rpc_response(url, method, params, token=token, timeout=timeout)[
        "result"
    ]

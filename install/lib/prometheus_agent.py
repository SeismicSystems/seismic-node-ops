#!/usr/bin/env python3
"""Root-side optional agent installation. No implicit service activation.

JSON settings contain no credentials and are also the installer's rerun state.
The node inventory contains the same non-secret settings. Keep, update, and
explicit disable are distinct operations; only disable may stop a service.
"""

from __future__ import annotations

import argparse
import grp
import hashlib
import json
import os
import platform
import pwd
import re
import shutil
import stat
import subprocess
import tarfile
import tempfile
import time
import urllib.parse
import urllib.request
from pathlib import Path

VERSION = "3.5.0"
CHECKSUMS = {
    "amd64": "e811827af26d822afb09a4f28314f61b618b12cff5369835a67f674d8b46f39a",
    "arm64": "173389cc42bf09c4e6e54cb53fa07a5a835d7c261e14775d2183181d6e385d1c",
}
HOME = Path("/etc/seismic/prometheus-agent")
SETTINGS = HOME / "installation.json"
TOKEN = HOME / "token"
CONFIG = HOME / "prometheus.yml"
BIN_DIR = Path(f"/usr/local/lib/seismic/prometheus-agent/{VERSION}")
SUPERVISOR_CONFIG = Path("/etc/supervisor/conf.d/prometheus-agent.conf")
LOG_DIR = Path("/var/log/seismic-prometheus-agent")
USER = "seismic-prometheus"
TEMPLATES = Path(__file__).resolve().parents[1] / "templates"
KEYS = {"enabled", "node", "role", "url", "data_dir", "version"}
NODE = re.compile(r"[a-z0-9](?:[a-z0-9.-]{0,251}[a-z0-9])?\Z")


class AgentError(Exception):
    pass


def safe_path(path: Path) -> None:
    """Reject symlinks and writable/non-root parents, including missing paths."""
    if not path.is_absolute() or str(path) != os.path.normpath(path):
        raise AgentError("Agent paths must be absolute and normalized")
    for part in [*reversed(path.parents), path]:
        try:
            metadata = part.lstat()
        except FileNotFoundError:
            continue
        if stat.S_ISLNK(metadata.st_mode):
            raise AgentError("Agent paths must not contain symbolic links")
        if part != path and (
            not stat.S_ISDIR(metadata.st_mode)
            or metadata.st_uid != 0
            or metadata.st_mode & 0o022
        ):
            raise AgentError(
                "Agent path parents must be root-owned and not writable by others"
            )


def root_file(path: Path) -> os.stat_result:
    safe_path(path)
    meta = path.lstat()
    if not stat.S_ISREG(meta.st_mode) or meta.st_uid != 0 or meta.st_mode & 0o022:
        raise AgentError("Expected a root-managed regular file")
    return meta


def validate(settings: dict, excluded: list[Path] = ()) -> dict:
    if set(settings) != KEYS or type(settings["enabled"]) is not bool:
        raise AgentError("Invalid agent settings schema")
    if settings["version"] != VERSION or settings["role"] not in (
        "validator",
        "observer",
    ):
        raise AgentError("Unsupported agent version or node role")
    node = settings["node"]
    if not isinstance(node, str) or not NODE.fullmatch(node) or ".." in node:
        raise AgentError("Agent node must be a lowercase hostname-style label")
    url = settings["url"]
    try:
        parsed = urllib.parse.urlsplit(url)
        valid_url = (
            isinstance(url, str)
            and url.isascii()
            and not re.search(r"[\s\\\"'<>]", url)
            and parsed.scheme == "https"
            and parsed.hostname
            and parsed.port in (None, 443)
            and parsed.username is None
            and parsed.password is None
            and parsed.path == "/api/v1/write"
            and not parsed.query
            and not parsed.fragment
            and "?" not in url
            and "#" not in url
        )
    except (ValueError, TypeError):
        valid_url = False
    if not valid_url:
        raise AgentError(
            "Remote write requires HTTPS port 443 and /api/v1/write, without credentials, query or fragment"
        )
    data = settings["data_dir"]
    if not isinstance(data, str) or not re.fullmatch(r"/[A-Za-z0-9_./-]+", data):
        raise AgentError("Unsafe agent WAL path")
    path = Path(data)
    if str(path) != data or os.path.normpath(data) != data:
        raise AgentError("Agent WAL path must be normalized")
    # WAL must not overlap node state/keys or any managed agent config/binaries.
    for other in [HOME, BIN_DIR.parent, LOG_DIR, Path("/etc"), Path("/usr"), *excluded]:
        if path == other or path in other.parents or other in path.parents:
            raise AgentError(
                "Agent WAL must be separate from node state, keys and configuration"
            )
    return settings


def load_settings() -> dict | None:
    if not SETTINGS.exists() and not SETTINGS.is_symlink():
        return None
    root_file(SETTINGS)
    try:
        return validate(json.loads(SETTINGS.read_text()))
    except (ValueError, TypeError, KeyError):
        raise AgentError("Invalid existing agent settings") from None


def read_token(path: Path) -> bytes:
    meta = root_file(path)
    if meta.st_size > 128 or meta.st_mode & 0o007:
        raise AgentError("Token file must be private and contain one opaque token")
    if path != TOKEN and meta.st_mode & 0o077:
        raise AgentError("Token handoff file must be root-only (0600)")
    token = path.read_bytes().strip()
    if not re.fullmatch(rb"[0-9a-f]{64}", token):
        raise AgentError("Expected a 64-character opaque monitoring token")
    return token + b"\n"


def atomic_write(path: Path, data: bytes, mode: int, gid: int = 0) -> None:
    safe_path(path)
    if path.exists():
        root_file(path)
    fd, name = tempfile.mkstemp(prefix=f".{path.name}.", dir=path.parent)
    try:
        with os.fdopen(fd, "wb") as stream:
            os.fchown(stream.fileno(), 0, gid)
            os.fchmod(stream.fileno(), mode)
            stream.write(data)
            stream.flush()
            os.fsync(stream.fileno())
        os.replace(name, path)
    finally:
        Path(name).unlink(missing_ok=True)


def directory(path: Path, uid: int = 0, gid: int = 0, mode: int = 0o755) -> None:
    safe_path(path)
    if path.exists():
        meta = path.lstat()
        if not stat.S_ISDIR(meta.st_mode) or meta.st_uid not in (0, uid):
            raise AgentError("Refusing to take over an existing agent directory")
    path.mkdir(parents=True, exist_ok=True)
    os.chown(path, uid, gid)
    os.chmod(path, mode)


def architecture(machine: str) -> str:
    try:
        return {"x86_64": "amd64", "aarch64": "arm64"}[machine]
    except KeyError:
        raise AgentError(
            "Prometheus Agent supports only Linux amd64 and arm64"
        ) from None


def install_release(tmp: Path) -> None:
    arch = architecture(platform.machine())
    filename = f"prometheus-{VERSION}.linux-{arch}.tar.gz"
    url = f"https://github.com/prometheus/prometheus/releases/download/v{VERSION}/{filename}"
    archive = tmp / filename
    digest = hashlib.sha256()
    size = 0
    with (
        urllib.request.urlopen(url, timeout=60) as response,
        archive.open("wb") as output,
    ):
        while chunk := response.read(1024 * 1024):
            size += len(chunk)
            if size > 400 * 1024 * 1024:
                raise AgentError("Prometheus release exceeds download limit")
            digest.update(chunk)
            output.write(chunk)
    if digest.hexdigest() != CHECKSUMS[arch]:
        raise AgentError("Prometheus release SHA-256 mismatch")
    directory(BIN_DIR)
    with tarfile.open(archive, "r:gz") as tar:
        for name in ("prometheus", "promtool"):
            member_name = f"prometheus-{VERSION}.linux-{arch}/{name}"
            matches = [m for m in tar.getmembers() if m.name == member_name]
            if (
                len(matches) != 1
                or not matches[0].isfile()
                or matches[0].size > 512 * 1024 * 1024
            ):
                raise AgentError("Invalid Prometheus release executable")
            # Never extract archive paths, links or permissions onto the host.
            with tar.extractfile(matches[0]) as stream:
                atomic_write(BIN_DIR / name, stream.read(), 0o755)


def supervisor_state() -> str:
    result = subprocess.run(
        ["/usr/bin/supervisorctl", "status", "prometheus-agent"],
        capture_output=True,
        text=True,
        check=False,
        timeout=15,
    )
    output = result.stdout.strip()
    if result.returncode in (0, 3):
        parts = output.split()
        if len(parts) >= 2 and parts[0] == "prometheus-agent":
            return parts[1]
    if "no such process" in output.lower():
        return "MISSING"
    # Never print subprocess output, which may contain operator configuration.
    raise AgentError(
        "Cannot determine agent state; inspect Supervisor before updating/disabling"
    )


def require_stopped() -> None:
    if (SUPERVISOR_CONFIG.exists() or SETTINGS.exists()) and supervisor_state() not in (
        "MISSING",
        "STOPPED",
        "FATAL",
    ):
        raise AgentError("Stop prometheus-agent before changing its configuration")


def render(settings: dict, token_path: Path = TOKEN) -> tuple[str, str]:
    validate(settings)
    config = (TEMPLATES / "prometheus-agent.yml").read_text()
    for key, value in {
        "NODE_PLACEHOLDER": settings["node"],
        "ROLE_PLACEHOLDER": settings["role"],
        "URL_PLACEHOLDER": json.dumps(settings["url"]),
        "TOKEN_PLACEHOLDER": json.dumps(str(token_path)),
    }.items():
        config = config.replace(key, value)
    service = (TEMPLATES / "supervisor/prometheus-agent.conf").read_text()
    service = service.replace("BINARY_PLACEHOLDER", str(BIN_DIR / "prometheus"))
    service = service.replace("DATA_PLACEHOLDER", settings["data_dir"])
    return config, service


def service_account():
    """Never grant token access to an existing shared primary group."""
    try:
        account = pwd.getpwnam(USER)
    except KeyError:
        subprocess.run(
            [
                "useradd",
                "--system",
                "--user-group",
                "--no-create-home",
                "--shell",
                "/usr/sbin/nologin",
                USER,
            ],
            check=True,
        )
        account = pwd.getpwnam(USER)
    if account.pw_uid == 0 or account.pw_gid == 0:
        raise AgentError("Agent account must be unprivileged")
    group = grp.getgrgid(account.pw_gid)
    if (
        group.gr_name != USER
        or set(group.gr_mem) - {USER, "root"}
        or any(p.pw_gid == account.pw_gid and p.pw_name != USER for p in pwd.getpwall())
    ):
        raise AgentError("Agent credentials require a dedicated private service group")
    return account


def require_managed_or_new() -> None:
    if load_settings() is None and (
        SUPERVISOR_CONFIG.exists() or SUPERVISOR_CONFIG.is_symlink()
    ):
        raise AgentError("Refusing to overwrite an unmanaged prometheus-agent program")


def install(settings: dict, token_source: Path) -> None:
    require_managed_or_new()
    token = read_token(token_source)
    require_stopped()
    account = service_account()
    directory(HOME, gid=account.pw_gid, mode=0o750)
    data = Path(settings["data_dir"])
    safe_path(data)
    previous = load_settings()
    if (
        data.exists()
        and any(data.iterdir())
        and (previous is None or previous["data_dir"] != str(data))
    ):
        raise AgentError("Refusing to adopt an unrelated nonempty WAL directory")
    directory(data, account.pw_uid, account.pw_gid, 0o750)
    directory(LOG_DIR)
    # Private staging protects the handoff even during validation failures.
    with tempfile.TemporaryDirectory(prefix=".install-", dir=HOME) as tmp_name:
        tmp = Path(tmp_name)
        install_release(tmp)
        staged_token = tmp / "token"
        staged_token.write_bytes(token)
        staged_token.chmod(0o600)
        config, service = render(settings, staged_token)
        staged_config = tmp / "prometheus.yml"
        staged_config.write_text(config)
        result = subprocess.run(
            [
                str(BIN_DIR / "promtool"),
                "check",
                "config",
                "--agent",
                str(staged_config),
            ],
            capture_output=True,
            check=False,
            timeout=30,
        )
        if result.returncode:
            raise AgentError(
                "Generated Prometheus agent configuration did not validate"
            )
        final_config, _ = render(settings)
        atomic_write(TOKEN, token, 0o640, account.pw_gid)
        atomic_write(CONFIG, final_config.encode(), 0o640, account.pw_gid)
        atomic_write(SUPERVISOR_CONFIG, service.encode(), 0o644)
        atomic_write(SETTINGS, (json.dumps(settings, indent=2) + "\n").encode(), 0o600)
    print("Prometheus Agent installed; no services were started.")


def disable() -> None:
    settings = load_settings()
    if settings is None:
        if SUPERVISOR_CONFIG.exists():
            raise AgentError(
                "Agent config exists without managed settings; refusing to remove it"
            )
        return
    deadline = time.monotonic() + 60
    while supervisor_state() not in ("MISSING", "STOPPED", "FATAL"):
        # EXITED may restart automatically; a NOT_RUNNING response can race
        # that transition. Only STOPPED/FATAL/MISSING are stable terminal states.
        result = subprocess.run(
            ["/usr/bin/supervisorctl", "stop", "prometheus-agent"],
            capture_output=True,
            check=False,
            timeout=60,
        )
        if result.returncode not in (0, 7) or time.monotonic() >= deadline:
            raise AgentError(
                "Could not confirm agent stop; disabling was not completed"
            )
        time.sleep(0.1)
    require_stopped()
    if SUPERVISOR_CONFIG.exists():
        root_file(SUPERVISOR_CONFIG)
        SUPERVISOR_CONFIG.unlink()
    settings["enabled"] = False
    atomic_write(SETTINGS, (json.dumps(settings, indent=2) + "\n").encode(), 0o600)
    print("Agent disabled; credentials and WAL preserved.")


def main() -> None:
    parser = argparse.ArgumentParser(description=__doc__)
    parser.add_argument(
        "action",
        choices=("show", "check", "check-existing", "install", "disable", "inventory"),
    )
    parser.add_argument("--field", choices=sorted(KEYS))
    parser.add_argument("--node")
    parser.add_argument("--role", choices=("validator", "observer"))
    parser.add_argument("--url")
    parser.add_argument("--data-dir")
    parser.add_argument("--token-source", type=Path)
    parser.add_argument("--exclude-path", action="append", type=Path, default=[])
    args = parser.parse_args()
    if os.geteuid() != 0:
        raise AgentError("Run the installer as root")
    if args.action in ("show", "inventory"):
        settings = load_settings()
        if args.action == "show":
            if settings is not None:
                print(
                    str(settings[args.field]).lower()
                    if isinstance(settings[args.field], bool)
                    else settings[args.field]
                )
        else:
            print("\n[monitoring]")
            for key, value in (settings or {"enabled": False}).items():
                print(f"{key} = {json.dumps(value)}")
        return
    if args.action == "disable":
        disable()
        return
    if args.action == "check-existing":
        settings = load_settings()
        if settings and settings["enabled"]:
            validate(settings, args.exclude_path)
            safe_path(Path(settings["data_dir"]))
            read_token(TOKEN)
            root_file(CONFIG)
            root_file(SUPERVISOR_CONFIG)
        return
    require_managed_or_new()
    settings = validate(
        {
            "enabled": True,
            "node": args.node,
            "role": args.role,
            "url": args.url,
            "data_dir": args.data_dir,
            "version": VERSION,
        },
        args.exclude_path,
    )
    safe_path(Path(settings["data_dir"]))
    if args.token_source is None:
        raise AgentError("A root-only token source file is required")
    if any(
        args.token_source == path or path in args.token_source.parents
        for path in args.exclude_path
    ):
        raise AgentError("Token handoff must be outside node data and key directories")
    read_token(args.token_source)
    if args.action == "install":
        install(settings, args.token_source)


if __name__ == "__main__":
    try:
        main()
    except AgentError as error:
        import sys

        print(f"Agent: {error}", file=sys.stderr)
        raise SystemExit(1) from None
    except (OSError, ValueError, subprocess.SubprocessError, shutil.Error):
        # Avoid echoing token contents, URLs or command output from exceptions.
        import sys

        print(
            "Agent operation refused or failed; check paths, permissions, settings and Supervisor state. No services were started.",
            file=sys.stderr,
        )
        raise SystemExit(1) from None

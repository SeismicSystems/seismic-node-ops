"""Optional Prometheus Agent installation driven by node.env.

The agent runs in ``MONITORING_MODE=push`` and its settings are the
``MONITORING_*`` keys in /etc/seismic/node.env; this module owns only the
artifacts derived from them: the pinned Prometheus release, the rendered
``prometheus.yml``, the ``targets.json`` listing the node's metrics endpoints,
and the bearer token(s) copied from root-only handoff files: Seismic's, and
optionally the operator's own for a second remote-write destination. The
static ``seismic-prometheus-agent.service`` unit is installed with every other
unit and reads ``MONITORING_DATA_DIR`` itself.

No action here starts a service. Disabling is an edit to node.env followed by
``seismic-node monitoring stop``; credentials and the WAL are preserved.
"""

from __future__ import annotations

import grp
import hashlib
import json
import os
import platform
import pwd
import re
import stat
import subprocess
import tarfile
import tempfile
import urllib.parse
import urllib.request
from pathlib import Path

from . import fsutil, nodeenv, systemd
from .errors import NodeError

VERSION = "3.5.0"
CHECKSUMS = {
    "amd64": "e811827af26d822afb09a4f28314f61b618b12cff5369835a67f674d8b46f39a",
    "arm64": "173389cc42bf09c4e6e54cb53fa07a5a835d7c261e14775d2183181d6e385d1c",
}
HOME = Path("/etc/seismic/prometheus-agent")
TOKEN = HOME / "token"
# Bearer token for the operator's own receiver; present only if one was given.
OPERATOR_TOKEN = HOME / "operator-token"
CONFIG = HOME / "prometheus.yml"
# Scraped by the single file_sd job in prometheus.yml; referenced there by a
# relative path, so validation can run against a staged copy.
TARGETS = HOME / "targets.json"
RELEASES = Path("/usr/local/lib/seismic/prometheus-agent")
BIN_DIR = RELEASES / VERSION
CURRENT = RELEASES / "current"
USER = "seismic-prometheus"
PROGRAM = "prometheus-agent"
TEMPLATE = (
    Path(__file__).resolve().parents[2] / "install/templates/prometheus-agent.yml"
)
NODE_NAME = re.compile(r"[a-z0-9](?:[a-z0-9.-]{0,251}[a-z0-9])?\Z")
DOWNLOAD_LIMIT = 400 * 1024 * 1024
EXECUTABLE_LIMIT = 512 * 1024 * 1024


class AgentError(NodeError):
    """Prometheus Agent installation was refused or failed."""


# ---------------------------------------------------------------------------
# Settings
# ---------------------------------------------------------------------------


def settings(env: nodeenv.NodeEnv) -> dict[str, str]:
    """Extract and strictly validate the agent's settings from node.env."""
    if env.monitoring_mode != "push":
        raise AgentError(
            f"MONITORING_MODE is {env.monitoring_mode} in node.env; the agent "
            "is only used in push mode"
        )
    node = env.string("MONITORING_NODE_NAME")
    if not NODE_NAME.fullmatch(node) or ".." in node:
        raise AgentError(
            "MONITORING_NODE_NAME must be a lowercase hostname-style label"
        )
    url = env.string("MONITORING_REMOTE_WRITE_URL")
    if not _valid_remote_write_url(url):
        raise AgentError(
            "MONITORING_REMOTE_WRITE_URL requires HTTPS port 443 and /api/v1/write, "
            "without credentials, query or fragment"
        )
    operator_url = env.string("MONITORING_OPERATOR_REMOTE_WRITE_URL")
    if operator_url and not _valid_remote_write_url(operator_url, any_port=True):
        raise AgentError(
            "MONITORING_OPERATOR_REMOTE_WRITE_URL requires HTTPS and /api/v1/write, "
            "without credentials, query or fragment"
        )
    if operator_url == url:
        raise AgentError(
            "MONITORING_OPERATOR_REMOTE_WRITE_URL must differ from MONITORING_REMOTE_WRITE_URL"
        )
    data_dir = env.path("MONITORING_DATA_DIR")
    if not re.fullmatch(r"/[A-Za-z0-9_./-]+", str(data_dir)):
        raise AgentError("MONITORING_DATA_DIR contains unsafe characters")
    for other in (HOME, RELEASES, Path("/etc"), Path("/usr")):
        if data_dir == other or other in data_dir.parents or data_dir in other.parents:
            raise AgentError(
                "MONITORING_DATA_DIR must be separate from agent configuration and binaries"
            )
    return {
        "node": node,
        "role": env.role,
        "url": url,
        "operator_url": operator_url,
        "data_dir": str(data_dir),
    }


def _valid_remote_write_url(url: str, *, any_port: bool = False) -> bool:
    if not url.isascii() or re.search(r"[\s\\\"'<>]", url):
        return False
    try:
        parsed = urllib.parse.urlsplit(url)
    except ValueError:
        return False
    return bool(
        parsed.scheme == "https"
        and parsed.hostname
        and (any_port or parsed.port in (None, 443))
        and parsed.username is None
        and parsed.password is None
        and parsed.path == "/api/v1/write"
        and not parsed.query
        and not parsed.fragment
        and "?" not in url
        and "#" not in url
    )


def read_token(path: Path, *, handoff: bool, operator: bool = False) -> bytes:
    """Read a bearer token; handoff files must be root-only (0600).

    Seismic's token is a 64-hex string. The operator's receiver is theirs, so
    its token is any single printable line.
    """
    meta = fsutil.require_root_managed_file(path, "Monitoring token")
    if meta.st_size > (1024 if operator else 128) or meta.st_mode & 0o007:
        raise AgentError("Token file must be private and contain one opaque token")
    if handoff and meta.st_mode & 0o077:
        raise AgentError("Token handoff file must be root-only (0600)")
    token = path.read_bytes().strip()
    if operator:
        if not token or not re.fullmatch(rb"[\x21-\x7e]+", token):
            raise AgentError(
                "Expected a single-line printable ASCII operator token without spaces"
            )
    elif not re.fullmatch(rb"[0-9a-f]{64}", token):
        raise AgentError("Expected a 64-character opaque monitoring token")
    return token + b"\n"


def scrape_targets(env: nodeenv.NodeEnv) -> list[dict[str, object]]:
    """The node's loopback metrics endpoints, in Prometheus file_sd form.

    Summit's and Reth's ports are fixed by their unit files; the Custodian's
    council listener serves /metrics on CUSTODIAN_COUNCIL_PORT; the
    checkpointer only exists when enabled. The same table, as /prom-* routes,
    is what the proxy publishes in pull mode.
    """
    services = [
        ("summit", nodeenv.SUMMIT_METRICS_PORT, "/"),
        ("reth", nodeenv.RETH_METRICS_PORT, "/"),
        ("custodian", env.integer("CUSTODIAN_COUNCIL_PORT"), "/metrics"),
    ]
    if env.flag("CHECKPOINTER_ENABLED"):
        services.append(("checkpointer", nodeenv.CHECKPOINTER_PORT, "/metrics"))
    return [
        {
            "targets": [f"127.0.0.1:{port}"],
            "labels": {"service": service, "__metrics_path__": path},
        }
        for service, port, path in services
    ]


def render_targets(env: nodeenv.NodeEnv) -> str:
    return json.dumps(scrape_targets(env), indent=2) + "\n"


def render_operator_remote_write(values: dict[str, str], *, token: bool) -> str:
    """The optional second remote_write entry; empty when not configured."""
    url = values["operator_url"]
    if not url:
        return ""
    lines = [
        "  - name: operator",
        f'    url: "{url}"',
    ]
    if token:
        lines += [
            "    authorization:",
            "      type: Bearer",
            f'      credentials_file: "{OPERATOR_TOKEN}"',
        ]
    lines += [
        "    remote_timeout: 30s",
        "    queue_config:",
        "      min_shards: 1",
        "      max_shards: 2",
        "      capacity: 10000",
        "      max_samples_per_send: 2000",
        "      batch_send_deadline: 5s",
        "      min_backoff: 1s",
        "      max_backoff: 30s",
        "      retry_on_http_429: true",
    ]
    return "\n".join(lines) + "\n"


def render(values: dict[str, str], *, operator_token: bool = False) -> str:
    config = TEMPLATE.read_text()
    for key, value in {
        "NODE_PLACEHOLDER": values["node"],
        "ROLE_PLACEHOLDER": values["role"],
        "URL_PLACEHOLDER": f'"{values["url"]}"',
        "TOKEN_PLACEHOLDER": f'"{TOKEN}"',
        "OPERATOR_REMOTE_WRITE_PLACEHOLDER\n": render_operator_remote_write(
            values, token=operator_token
        ),
    }.items():
        config = config.replace(key, value)
    if "_PLACEHOLDER" in config:
        raise AgentError("Agent template has an unrendered placeholder")
    return config


# ---------------------------------------------------------------------------
# Filesystem
# ---------------------------------------------------------------------------


def atomic_write(path: Path, data: bytes, mode: int, gid: int = 0) -> None:
    """Replace a root-owned file atomically, optionally group-readable."""
    fsutil.require_absolute(path, "Agent file")
    fsutil.require_secure_root_parent(path, "Agent file")
    if path.exists() or path.is_symlink():
        fsutil.require_root_managed_file(path, "Agent file")
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
    fsutil.require_absolute(path, "Agent directory")
    if path.exists() or path.is_symlink():
        meta = path.lstat()
        if not stat.S_ISDIR(meta.st_mode) or meta.st_uid not in (0, uid):
            raise AgentError(f"Refusing to take over an existing directory: {path}")
    path.mkdir(parents=True, exist_ok=True)
    os.chown(path, uid, gid)
    os.chmod(path, mode)


def service_account() -> pwd.struct_passwd:
    """Create the agent account; never share a primary group with others."""
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


# ---------------------------------------------------------------------------
# Release
# ---------------------------------------------------------------------------


def architecture(machine: str) -> str:
    try:
        return {"x86_64": "amd64", "aarch64": "arm64"}[machine]
    except KeyError:
        raise AgentError(
            "Prometheus Agent supports only Linux amd64 and arm64"
        ) from None


def install_release(tmp: Path) -> None:
    """Download the pinned release, verify it, and point ``current`` at it."""
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
            if size > DOWNLOAD_LIMIT:
                raise AgentError("Prometheus release exceeds download limit")
            digest.update(chunk)
            output.write(chunk)
    if digest.hexdigest() != CHECKSUMS[arch]:
        raise AgentError("Prometheus release SHA-256 mismatch")
    directory(RELEASES)
    directory(BIN_DIR)
    with tarfile.open(archive, "r:gz") as tar:
        for name in ("prometheus", "promtool"):
            member_name = f"prometheus-{VERSION}.linux-{arch}/{name}"
            matches = [m for m in tar.getmembers() if m.name == member_name]
            if (
                len(matches) != 1
                or not matches[0].isfile()
                or matches[0].size > EXECUTABLE_LIMIT
            ):
                raise AgentError("Invalid Prometheus release executable")
            # Never extract archive paths, links or permissions onto the host.
            with tar.extractfile(matches[0]) as stream:
                atomic_write(BIN_DIR / name, stream.read(), 0o755)
    point_current(BIN_DIR)


def point_current(target: Path) -> None:
    """Atomically retarget the ``current`` symlink the unit executes."""
    if CURRENT.exists() and not CURRENT.is_symlink():
        raise AgentError(f"{CURRENT} exists and is not a symbolic link")
    staging = RELEASES / f".current.{os.getpid()}"
    staging.unlink(missing_ok=True)
    staging.symlink_to(target.name)
    os.replace(staging, CURRENT)


# ---------------------------------------------------------------------------
# Actions
# ---------------------------------------------------------------------------


def require_stopped() -> None:
    value = systemd.status(PROGRAM)
    if value.exists and value.state not in systemd.STOPPED_STATES:
        raise AgentError("Stop the Prometheus Agent before changing its configuration")


def install(
    env: nodeenv.NodeEnv, token_source: Path, operator_token_source: Path | None = None
) -> None:
    """Install or update the agent from node.env; nothing is started."""
    values = settings(env)
    if operator_token_source is not None and not values["operator_url"]:
        raise AgentError(
            "--operator-token-source needs MONITORING_OPERATOR_REMOTE_WRITE_URL in node.env"
        )
    for key in ("RETH_DATA_DIR", "SUMMIT_DATA_DIR", "KEYS_DIR"):
        node_dir = env.path(key)
        for source in (token_source, operator_token_source):
            if source is not None and (source == node_dir or node_dir in source.parents):
                raise AgentError(
                    "Token handoff must be outside node data and key directories"
                )
    token = read_token(token_source, handoff=True)
    operator_token = (
        None
        if operator_token_source is None
        else read_token(operator_token_source, handoff=True, operator=True)
    )
    require_stopped()
    account = service_account()
    directory(HOME, gid=account.pw_gid, mode=0o750)
    data = Path(values["data_dir"])
    previous = CONFIG.exists()
    if data.exists() and any(data.iterdir()) and not previous:
        raise AgentError("Refusing to adopt an unrelated nonempty WAL directory")
    directory(data, account.pw_uid, account.pw_gid, 0o750)
    # Private staging protects the handoff even during validation failures.
    with tempfile.TemporaryDirectory(prefix=".install-", dir=HOME) as tmp_name:
        tmp = Path(tmp_name)
        install_release(tmp)
        config = render(values, operator_token=operator_token is not None)
        targets = render_targets(env)
        staged_config = tmp / "prometheus.yml"
        staged_config.write_text(config)
        (tmp / TARGETS.name).write_text(targets)
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
        atomic_write(TOKEN, token, 0o640, account.pw_gid)
        if operator_token is not None:
            atomic_write(OPERATOR_TOKEN, operator_token, 0o640, account.pw_gid)
        elif OPERATOR_TOKEN.exists() or OPERATOR_TOKEN.is_symlink():
            # The rendered config no longer references it; do not leave a
            # stale credential behind.
            fsutil.require_root_managed_file(OPERATOR_TOKEN, "Operator token")
            OPERATOR_TOKEN.unlink()
        atomic_write(TARGETS, targets.encode(), 0o640, account.pw_gid)
        atomic_write(CONFIG, config.encode(), 0o640, account.pw_gid)
    print("Prometheus Agent installed; no services were started.")


def check_existing(env: nodeenv.NodeEnv) -> None:
    """Verify the installed artifacts match node.env."""
    values = settings(env)
    read_token(TOKEN, handoff=False)
    operator_token = bool(values["operator_url"]) and OPERATOR_TOKEN.exists()
    if operator_token:
        read_token(OPERATOR_TOKEN, handoff=False, operator=True)
    fsutil.require_root_managed_file(CONFIG, "Agent configuration")
    if CONFIG.read_text() != render(values, operator_token=operator_token):
        raise AgentError(
            "Installed prometheus.yml does not match node.env; reinstall the agent"
        )
    fsutil.require_root_managed_file(TARGETS, "Agent scrape targets")
    if TARGETS.read_text() != render_targets(env):
        raise AgentError(
            "Installed targets.json does not match node.env; reinstall the agent"
        )
    if not CURRENT.is_symlink() or not (CURRENT / "prometheus").is_file():
        raise AgentError("Prometheus Agent binary is not installed")
    data = Path(values["data_dir"])
    fsutil.require_directory(data, "Agent WAL directory")

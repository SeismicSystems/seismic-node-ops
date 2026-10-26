"""Read and validate ``/etc/seismic/node.env``.

``node.env`` is the single operator-authored configuration for a node. systemd
units load it through ``EnvironmentFile=``; this module lets Python tooling
read the same file and see exactly the values systemd sees.

Parsing follows systemd's EnvironmentFile grammar, verified against systemd
257 (``systemd-run -p EnvironmentFile=``):

* Blank lines and lines whose first non-blank character is ``#`` or ``;`` are
  comments.
* ``KEY=value``; whitespace around ``=`` is ignored. Keys match
  ``[A-Za-z_][A-Za-z0-9_]*``.
* Bare values are trimmed. A backslash escapes the next character (the
  backslash is dropped). A trailing backslash continues onto the next line
  with no separator. ``#`` inside a value is *not* a comment.
* Double-quoted values honour ``\\"`` and ``\\\\``; every other backslash is
  literal (``"\\n"`` stays two characters). They may span lines.
* Single-quoted values are fully literal and may span lines.

Where systemd silently tolerates a construct whose outcome is surprising,
this parser rejects it so the file can never contain something the two
readers would interpret differently: invalid keys, missing ``=``, text after
a closing quote, unterminated quotes, and duplicate keys are all errors.

The schema deliberately has no defaults. systemd cannot derive one key from
another, so the installer writes every applicable key and this module
requires them. Defaults live only in ``install/templates/node.env.example``.
"""

from __future__ import annotations

import os
import re
from collections.abc import Callable
from dataclasses import dataclass
from pathlib import Path
from typing import Any

from .errors import NodeError
from .fsutil import (
    PUBLIC_KEY,
    require_absolute,
    require_root_managed_file,
    require_separate_paths,
)

DEFAULT_PATH = Path("/etc/seismic/node.env")
# Fixed by the static systemd units; not configurable through node.env.
SERVICE_USER = "seismic"
SERVICE_GROUP = "seismic"
CHECKPOINT_START_CONFIG = Path("/etc/seismic/checkpoint-start.toml")
WEAK_SUBJECTIVITY_PATH = Path("/etc/seismic/weak-subjectivity.toml")
# systemd forbids a variable as the program in ExecStart=, so binaries have
# fixed locations that the units name literally.
BINARIES = {
    "summit": Path("/usr/local/bin/summit"),
    "reth": Path("/usr/local/bin/seismic-reth"),
    "custodian": Path("/usr/local/bin/seismic-centralized-custodian-service"),
    "checkpointer": Path("/usr/local/bin/seismic-checkpointer"),
    "mdbx_copy": Path("/usr/local/bin/mdbx_copy"),
    "summit_checkpoint_runner": Path(
        "/usr/local/libexec/seismic/summit-checkpoint-runner"
    ),
    "prometheus_agent": Path(
        "/usr/local/lib/seismic/prometheus-agent/current/prometheus"
    ),
}

# Loopback metrics ports fixed by the unit files (Summit --prom-port, Reth
# --metrics) and the checkpointer's --port. The Custodian's is
# CUSTODIAN_COUNCIL_PORT in node.env.
SUMMIT_METRICS_PORT = 9090
RETH_METRICS_PORT = 9001
CHECKPOINTER_PORT = 42069

KEY_PATTERN = re.compile(r"[A-Za-z_][A-Za-z0-9_]*")
ROLES = ("validator", "observer")
INSTALL_METHODS = ("download", "prebuilt", "source")
TLS_MODES = ("disabled", "custodian-only", "full", "external")
MANAGED_TLS_MODES = ("custodian-only", "full")
# How metrics leave the node: not at all, pushed by the Prometheus Agent, or
# published as JWT-protected /prom-* routes for Seismic to pull. Seismic
# monitors validators only; an observer's agent can push to the operator's own
# Prometheus but never to Seismic, and observers cannot use pull. An empty
# value selects the role's default.
MONITORING_MODES = ("off", "push", "pull")
MONITORING_DEFAULTS = {"validator": "push", "observer": "off"}
# Pull needs an HTTPS proxy on the node: managed OpenResty (either mode; the
# /prom-* routes do not depend on the node's RPC being public) or the
# operator's own proxy in `external` mode, which then has to enforce the same
# JWT check itself.
PULL_TLS_MODES = ("custodian-only", "full", "external")


class NodeEnvError(NodeError):
    """Raised for unreadable, malformed, or invalid node.env contents."""


# ---------------------------------------------------------------------------
# Parsing
# ---------------------------------------------------------------------------


def _fail(line_number: int, message: str) -> NodeEnvError:
    return NodeEnvError(f"node.env line {line_number}: {message}")


def _parse_bare(lines: list[str], index: int, raw: str) -> tuple[str, int]:
    """Parse an unquoted value starting at ``raw``; return (value, next index)."""
    out: list[str] = []
    current = raw
    while True:
        position = 0
        continued = False
        while position < len(current):
            char = current[position]
            if char == "\\":
                if position + 1 < len(current):
                    out.append(current[position + 1])
                    position += 2
                    continue
                continued = True
                break
            out.append(char)
            position += 1
        if not continued:
            break
        index += 1
        if index >= len(lines):
            # systemd drops a trailing continuation at EOF.
            break
        current = lines[index]
    return "".join(out).rstrip(), index


def _parse_quoted(
    lines: list[str], index: int, raw: str, quote: str, start_line: int
) -> tuple[str, int]:
    """Parse a quoted value; ``raw`` begins just after the opening quote."""
    out: list[str] = []
    current = raw
    while True:
        position = 0
        while position < len(current):
            char = current[position]
            if quote == '"' and char == "\\" and position + 1 < len(current):
                following = current[position + 1]
                if following in ('"', "\\"):
                    out.append(following)
                    position += 2
                    continue
            if char == quote:
                remainder = current[position + 1 :].strip()
                if remainder:
                    raise _fail(
                        start_line,
                        f"unexpected text after closing quote: {remainder!r}",
                    )
                return "".join(out), index
            out.append(char)
            position += 1
        index += 1
        if index >= len(lines):
            raise _fail(start_line, f"unterminated {quote} quote")
        out.append("\n")
        current = lines[index]


def parse(text: str) -> dict[str, str]:
    """Parse node.env text into an ordered mapping of key to value."""
    values: dict[str, str] = {}
    lines = text.split("\n")
    index = 0
    while index < len(lines):
        line_number = index + 1
        line = lines[index]
        stripped = line.strip()
        if not stripped or stripped[0] in "#;":
            index += 1
            continue
        if "=" not in stripped:
            raise _fail(line_number, "expected KEY=value")
        key, _, rest = stripped.partition("=")
        key = key.strip()
        if not KEY_PATTERN.fullmatch(key):
            raise _fail(line_number, f"invalid key {key!r}")
        if key in values:
            raise _fail(line_number, f"duplicate key {key}")
        rest = rest.lstrip()
        if rest[:1] in ('"', "'"):
            value, index = _parse_quoted(lines, index, rest[1:], rest[0], line_number)
        else:
            value, index = _parse_bare(lines, index, rest)
        values[key] = value
        index += 1
    return values


# ---------------------------------------------------------------------------
# Schema
# ---------------------------------------------------------------------------

Validator = Callable[[str, str], Any]


def _string(value: str, key: str) -> str:
    if not value:
        raise NodeEnvError(f"node.env {key} must not be empty")
    return value


def _optional_string(value: str, key: str) -> str:
    return value


VERSION_TAG = re.compile(r"v[0-9]+\.[0-9]+\.[0-9]+(-[0-9A-Za-z.]+)?")
BRANCH_REF = re.compile(r"refs/heads/[A-Za-z0-9][A-Za-z0-9._/-]*")


def _version(value: str, key: str) -> str:
    """A release tag, or an explicit branch ref for source builds."""
    if VERSION_TAG.fullmatch(value) or BRANCH_REF.fullmatch(value):
        return value
    raise NodeEnvError(
        f"node.env {key} must be a release tag like v0.1.0 (or refs/heads/<branch> "
        f"for source builds)"
    )


def _path(value: str, key: str) -> Path:
    if not value:
        raise NodeEnvError(f"node.env {key} must not be empty")
    path = Path(value)
    require_absolute(path, f"node.env {key}")
    return path


def _optional_path(value: str, key: str) -> Path | None:
    return _path(value, key) if value else None


def _int(minimum: int, maximum: int) -> Validator:
    def validate(value: str, key: str) -> int:
        if not re.fullmatch(r"[0-9]+", value):
            raise NodeEnvError(f"node.env {key} must be a non-negative integer")
        number = int(value)
        if not minimum <= number <= maximum:
            raise NodeEnvError(
                f"node.env {key} must be between {minimum} and {maximum}"
            )
        return number

    return validate


def _bool(value: str, key: str) -> bool:
    if value == "true":
        return True
    if value == "false":
        return False
    raise NodeEnvError(f"node.env {key} must be exactly 'true' or 'false'")


def _enum(choices: tuple[str, ...]) -> Validator:
    def validate(value: str, key: str) -> str:
        if value not in choices:
            raise NodeEnvError(f"node.env {key} must be one of: {', '.join(choices)}")
        return value

    return validate


def _bind_address(value: str, key: str) -> str:
    if not re.fullmatch(r"[0-9]{1,3}(\.[0-9]{1,3}){3}", value):
        raise NodeEnvError(f"node.env {key} must be an IPv4 address")
    return value


def _host_port(value: str, key: str) -> str:
    host, _, port = value.rpartition(":")
    if not host or not re.fullmatch(r"[0-9]+", port) or not 1 <= int(port) <= 65535:
        raise NodeEnvError(f"node.env {key} must be host:port")
    return value


def _https_url(value: str, key: str) -> str:
    if not re.fullmatch(r"https://[^\s/]+(/[^\s]*)?", value):
        raise NodeEnvError(f"node.env {key} must be an https:// URL")
    return value


def _optional_https_url(value: str, key: str) -> str:
    return _https_url(value, key) if value else value


def _optional_url(value: str, key: str) -> str:
    if value and not re.fullmatch(r"(https?|enode)://[^\s]+", value):
        raise NodeEnvError(f"node.env {key} must be a URL or empty")
    return value


def _public_key(value: str, key: str) -> str:
    if not PUBLIC_KEY.fullmatch(value):
        raise NodeEnvError(f"node.env {key} must be 64 lowercase hex characters")
    return value


def _eth_address(value: str, key: str) -> str:
    if not re.fullmatch(r"0x[0-9a-fA-F]{40}", value):
        raise NodeEnvError(f"node.env {key} must be a 0x-prefixed 20-byte address")
    return value


def _domain(value: str, key: str) -> str:
    if not re.fullmatch(
        r"[a-z0-9]([a-z0-9-]*[a-z0-9])?(\.[a-z0-9]([a-z0-9-]*[a-z0-9])?)+", value
    ):
        raise NodeEnvError(f"node.env {key} must be a lowercase DNS name")
    return value


# Conditions decide whether a key applies to this file. ``raw`` holds parsed
# strings; conditions only inspect keys that are always required, so they are
# safe to evaluate before full validation.
Condition = Callable[[dict[str, str]], bool]


def always(raw: dict[str, str]) -> bool:
    return True


def observer(raw: dict[str, str]) -> bool:
    return raw.get("SEISMIC_ROLE") == "observer"


def validator(raw: dict[str, str]) -> bool:
    return raw.get("SEISMIC_ROLE") == "validator"


def flag(name: str) -> Condition:
    return lambda raw: raw.get(name) == "true"


def both(*conditions: Condition) -> Condition:
    return lambda raw: all(condition(raw) for condition in conditions)


def prebuilt(component: str) -> Condition:
    return lambda raw: raw.get(f"{component}_INSTALL_METHOD") == "prebuilt"


def tls_managed(raw: dict[str, str]) -> bool:
    return raw.get("TLS_MODE") in MANAGED_TLS_MODES


def tls_external(raw: dict[str, str]) -> bool:
    return raw.get("TLS_MODE") == "external"


def monitoring(mode: str) -> Condition:
    return lambda raw: raw.get("MONITORING_MODE") == mode


@dataclass(frozen=True)
class Field:
    validate: Validator
    applies: Condition = always


SCHEMA: dict[str, Field] = {
    # Identity
    "SEISMIC_ROLE": Field(_enum(ROLES)),
    # Persistent layout
    "RETH_DATA_DIR": Field(_path),
    "SUMMIT_DATA_DIR": Field(_path),
    "KEYS_DIR": Field(_path),
    "RETH_P2P_KEY_PATH": Field(_path),
    "SUMMIT_KEYS_DIR": Field(_path),
    "SUMMIT_CRITICAL_LOG_DIR": Field(_path),
    "SUMMIT_BOOTSTRAPPERS_FILE": Field(_optional_path),
    "SUMMIT_BOOTSTRAPPERS_ARG": Field(_optional_string),
    # Binaries
    "SUMMIT_INSTALL_METHOD": Field(_enum(INSTALL_METHODS)),
    "RETH_INSTALL_METHOD": Field(_enum(INSTALL_METHODS)),
    "SUMMIT_VERSION": Field(_version),
    "RETH_VERSION": Field(_version),
    "SUMMIT_PREBUILT_PATH": Field(_path, prebuilt("SUMMIT")),
    "RETH_PREBUILT_PATH": Field(_path, prebuilt("RETH")),
    # The checkpointer's mdbx_copy ships in Reth's tarball and builds from
    # Reth's checkout; a prebuilt Reth needs it supplied alongside.
    "RETH_MDBX_COPY_PREBUILT_PATH": Field(
        _path, both(flag("CHECKPOINTER_ENABLED"), prebuilt("RETH"))
    ),
    # Network
    "GENESIS_PATH": Field(_path),
    "RETH_BOOTNODE": Field(_optional_url),
    "RETH_BOOTNODES_ARG": Field(_optional_string),
    "RETH_RPC_BIND": Field(_bind_address),
    "SUMMIT_RPC_BIND": Field(_bind_address),
    "RETH_ENGINE_IPC": Field(_path),
    "OBSERVER_PARENT_NODE_PUBLIC_KEY": Field(_public_key, observer),
    "OBSERVER_INDEX": Field(_int(0, 2**31 - 1), observer),
    "OBSERVER_PUBLIC_ADDRESS": Field(_host_port, observer),
    "OBSERVER_P2P_PORT": Field(_int(1, 65535), observer),
    # Custodian (always present)
    "CUSTODIAN_DATA_DIR": Field(_path),
    "CUSTODIAN_INSTALL_METHOD": Field(_enum(INSTALL_METHODS)),
    "CUSTODIAN_VERSION": Field(_version),
    "CUSTODIAN_PREBUILT_PATH": Field(_path, prebuilt("CUSTODIAN")),
    "CUSTODIAN_SOCKET": Field(_path),
    "CUSTODIAN_COUNCIL_PORT": Field(_int(1, 65535)),
    "CUSTODIAN_COUNCIL_ADDRESS": Field(_eth_address),
    "CUSTODIAN_CHAIN_ID": Field(_int(1, 2**63 - 1)),
    "CUSTODIAN_PARENT_URL": Field(_https_url, observer),
    # Checkpointer
    "CHECKPOINTER_ENABLED": Field(_bool),
    "CHECKPOINTS_DIR": Field(_path, flag("CHECKPOINTER_ENABLED")),
    "CHECKPOINTER_INSTALL_METHOD": Field(
        _enum(INSTALL_METHODS), flag("CHECKPOINTER_ENABLED")
    ),
    "CHECKPOINTER_VERSION": Field(_version, flag("CHECKPOINTER_ENABLED")),
    "CHECKPOINTER_PREBUILT_PATH": Field(
        _path, both(flag("CHECKPOINTER_ENABLED"), prebuilt("CHECKPOINTER"))
    ),
    "CHECKPOINTER_CONFIG_PATH": Field(_path, flag("CHECKPOINTER_ENABLED")),
    # HTTPS endpoint
    "TLS_MODE": Field(_enum(TLS_MODES)),
    "DOMAIN": Field(_domain, tls_managed),
    "CUSTODIAN_PUBLIC_URL": Field(_https_url, both(tls_external)),
    "RATE_LIMIT_RPS": Field(_int(1, 1_000_000), tls_managed),
    "RATE_LIMIT_BURST": Field(_int(1, 1_000_000), tls_managed),
    "OPENRESTY_JWT_SECRET_PATH": Field(_path, tls_managed),
    # Monitoring
    "MONITORING_MODE": Field(_enum(MONITORING_MODES)),
    "MONITORING_NODE_NAME": Field(_string, monitoring("push")),
    # Only validators push to Seismic.
    "MONITORING_REMOTE_WRITE_URL": Field(
        _https_url, both(validator, monitoring("push"))
    ),
    # Optional for validators; an observer's agent has no other destination.
    "MONITORING_OPERATOR_REMOTE_WRITE_URL": Field(
        _optional_https_url, monitoring("push")
    ),
    "MONITORING_DATA_DIR": Field(_path, monitoring("push")),
}

# Keys whose conditions are evaluated must themselves be unconditional.
_CONDITION_INPUTS = (
    "SEISMIC_ROLE",
    "CHECKPOINTER_ENABLED",
    "MONITORING_MODE",
    "TLS_MODE",
    "SUMMIT_INSTALL_METHOD",
    "RETH_INSTALL_METHOD",
    "CUSTODIAN_INSTALL_METHOD",
    "CHECKPOINTER_INSTALL_METHOD",
)


# ---------------------------------------------------------------------------
# Validation
# ---------------------------------------------------------------------------


@dataclass(frozen=True)
class NodeEnv:
    """Validated node configuration with typed accessors."""

    raw: dict[str, str]
    values: dict[str, Any]
    source: Path | None = None

    @property
    def role(self) -> str:
        return self.values["SEISMIC_ROLE"]

    @property
    def monitoring_mode(self) -> str:
        return self.values["MONITORING_MODE"]

    def applies(self, key: str) -> bool:
        return key in self.values

    def get(self, key: str) -> Any:
        try:
            return self.values[key]
        except KeyError:
            raise NodeEnvError(
                f"node.env {key} does not apply to this configuration"
            ) from None

    def path(self, key: str) -> Path:
        value = self.get(key)
        if not isinstance(value, Path):
            raise NodeEnvError(f"node.env {key} is not a path")
        return value

    def optional_path(self, key: str) -> Path | None:
        value = self.get(key)
        if value is not None and not isinstance(value, Path):
            raise NodeEnvError(f"node.env {key} is not a path")
        return value

    def string(self, key: str) -> str:
        value = self.get(key)
        if not isinstance(value, str):
            raise NodeEnvError(f"node.env {key} is not a string")
        return value

    def integer(self, key: str) -> int:
        value = self.get(key)
        if isinstance(value, bool) or not isinstance(value, int):
            raise NodeEnvError(f"node.env {key} is not an integer")
        return value

    def flag(self, key: str) -> bool:
        value = self.get(key)
        if not isinstance(value, bool):
            raise NodeEnvError(f"node.env {key} is not a boolean")
        return value


def validate(raw: dict[str, str], *, source: Path | None = None) -> NodeEnv:
    """Validate parsed values against the schema and cross-key rules."""
    try:
        return _validate(raw, source)
    except NodeEnvError:
        raise
    except NodeError as error:
        raise NodeEnvError(str(error)) from error


def _validate(raw: dict[str, str], source: Path | None) -> NodeEnv:
    unknown = sorted(set(raw) - set(SCHEMA))
    if unknown:
        raise NodeEnvError(f"node.env has unknown keys: {', '.join(unknown)}")

    # An empty MONITORING_MODE means the role's default. Resolve it before the
    # conditions run, so the MONITORING_* keys are gated on the real mode.
    # install.sh receives the resolved value through ``shell_exports``.
    if raw.get("MONITORING_MODE") == "":
        raw = dict(raw)
        raw["MONITORING_MODE"] = MONITORING_DEFAULTS.get(
            raw.get("SEISMIC_ROLE", ""), ""
        )

    # Condition inputs are validated first so conditions see sane values.
    values: dict[str, Any] = {}
    for key in _CONDITION_INPUTS:
        field = SCHEMA[key]
        if key not in raw:
            if field.applies(raw):
                raise NodeEnvError(f"node.env is missing required key {key}")
            continue
        if field.applies(raw):
            values[key] = field.validate(raw[key], key)

    missing: list[str] = []
    for key, field in SCHEMA.items():
        if key in values:
            continue
        if not field.applies(raw):
            continue
        if key not in raw:
            missing.append(key)
            continue
        values[key] = field.validate(raw[key], key)
    if missing:
        raise NodeEnvError(f"node.env is missing required keys: {', '.join(missing)}")

    env = NodeEnv(raw=dict(raw), values=values, source=source)
    _validate_layout(env)
    _validate_derived(env)
    return env


def _validate_derived(env: NodeEnv) -> None:
    """Derived keys exist only because systemd cannot compute them; they must
    agree with the values they are derived from."""
    for component in ("SUMMIT", "RETH", "CUSTODIAN", "CHECKPOINTER"):
        method_key = f"{component}_INSTALL_METHOD"
        if env.applies(method_key) and env.string(method_key) == "download":
            version = env.string(f"{component}_VERSION")
            if not VERSION_TAG.fullmatch(version):
                raise NodeEnvError(
                    f"node.env {component}_VERSION must be a release tag when "
                    f"{method_key}=download; branches are not downloadable"
                )
    if env.applies("RETH_MDBX_COPY_PREBUILT_PATH") and env.path(
        "RETH_MDBX_COPY_PREBUILT_PATH"
    ) == env.path("RETH_PREBUILT_PATH"):
        raise NodeEnvError(
            "node.env RETH_MDBX_COPY_PREBUILT_PATH must name mdbx_copy, not the "
            "Reth binary in RETH_PREBUILT_PATH"
        )
    _require_flag_arg(
        env, "RETH_BOOTNODES_ARG", "--bootnodes", env.string("RETH_BOOTNODE")
    )
    bootstrappers = env.optional_path("SUMMIT_BOOTSTRAPPERS_FILE")
    _require_flag_arg(
        env,
        "SUMMIT_BOOTSTRAPPERS_ARG",
        "--bootstrappers",
        "" if bootstrappers is None else str(bootstrappers),
    )

    if env.role == "observer":
        address = env.string("OBSERVER_PUBLIC_ADDRESS")
        port = int(address.rpartition(":")[2])
        if env.integer("OBSERVER_P2P_PORT") != port:
            raise NodeEnvError(
                f"node.env OBSERVER_P2P_PORT must be {port} to match OBSERVER_PUBLIC_ADDRESS"
            )

    # Seismic monitors validators only. An observer may run the agent for the
    # operator's own Prometheus, so push needs that destination; pull would
    # publish routes nobody scrapes.
    if env.role == "observer":
        if env.monitoring_mode == "pull":
            raise NodeEnvError(
                "node.env MONITORING_MODE=pull is not available to observers: Seismic "
                "does not scrape observers; set off, or push with "
                "MONITORING_OPERATOR_REMOTE_WRITE_URL for your own Prometheus"
            )
        if env.monitoring_mode == "push" and not env.string(
            "MONITORING_OPERATOR_REMOTE_WRITE_URL"
        ):
            raise NodeEnvError(
                "node.env MONITORING_MODE=push on an observer needs "
                "MONITORING_OPERATOR_REMOTE_WRITE_URL: observers never push to "
                "Seismic, so the agent has no other destination"
            )

    # A validator's Custodian serves purpose keys to its observers and must be
    # reachable; an observer's only fetches from the parent.
    if env.role == "validator" and env.string("TLS_MODE") == "disabled":
        raise NodeEnvError(
            "node.env TLS_MODE=disabled is only valid for observers: a validator "
            "must expose its Custodian; set TLS_MODE to custodian-only, full, or "
            "external"
        )

    if env.monitoring_mode == "pull" and env.string("TLS_MODE") not in PULL_TLS_MODES:
        raise NodeEnvError(
            "node.env MONITORING_MODE=pull needs an HTTPS proxy to publish the "
            "/prom-* routes: set TLS_MODE to custodian-only or full (managed "
            "OpenResty) or external (your proxy must enforce the JWT check)"
        )


def _require_flag_arg(env: NodeEnv, key: str, flag_name: str, value: str) -> None:
    # systemd splits an unquoted $KEY on whitespace, so the value must be a
    # single word for the expansion to yield exactly two arguments.
    if re.search(r"\s", value):
        raise NodeEnvError(
            f"node.env value for {key} must not contain whitespace: {value!r}"
        )
    expected = f"{flag_name} {value}" if value else ""
    if env.string(key) != expected:
        raise NodeEnvError(f"node.env {key} must be exactly {expected!r}")


def _validate_layout(env: NodeEnv) -> None:
    """Enforce path relationships the units and tooling rely on."""
    keys_dir = env.path("KEYS_DIR")
    for key in ("RETH_P2P_KEY_PATH", "SUMMIT_KEYS_DIR"):
        if not _within(env.path(key), keys_dir):
            raise NodeEnvError(
                f"node.env {key} must be inside KEYS_DIR: {env.path(key)}"
            )

    if not _within(env.path("SUMMIT_CRITICAL_LOG_DIR"), env.path("SUMMIT_DATA_DIR")):
        raise NodeEnvError(
            "node.env SUMMIT_CRITICAL_LOG_DIR must be inside SUMMIT_DATA_DIR"
        )

    top_level = {
        "RETH_DATA_DIR": env.path("RETH_DATA_DIR"),
        "SUMMIT_DATA_DIR": env.path("SUMMIT_DATA_DIR"),
        "KEYS_DIR": keys_dir,
    }
    top_level["CUSTODIAN_DATA_DIR"] = env.path("CUSTODIAN_DATA_DIR")
    for key in ("CHECKPOINTS_DIR", "MONITORING_DATA_DIR"):
        if env.applies(key):
            top_level[key] = env.path(key)
    require_separate_paths(top_level)

    require_separate_paths(
        {
            "RETH_P2P_KEY_PATH": env.path("RETH_P2P_KEY_PATH"),
            "SUMMIT_KEYS_DIR": env.path("SUMMIT_KEYS_DIR"),
        }
    )

    bootstrappers = env.optional_path("SUMMIT_BOOTSTRAPPERS_FILE")
    if bootstrappers is not None and not _within(
        bootstrappers, env.path("SUMMIT_KEYS_DIR")
    ):
        raise NodeEnvError(
            "node.env SUMMIT_BOOTSTRAPPERS_FILE must be inside SUMMIT_KEYS_DIR"
        )


def _within(path: Path, parent: Path) -> bool:
    try:
        path.relative_to(parent)
    except ValueError:
        return False
    return path != parent


# ---------------------------------------------------------------------------
# Loading
# ---------------------------------------------------------------------------


def read_text(path: Path) -> str:
    """Read node.env with the same ownership policy as other root-managed files."""
    require_root_managed_file(path, "node.env")
    try:
        file_descriptor = os.open(path, os.O_RDONLY | os.O_NOFOLLOW)
    except OSError as error:
        raise NodeEnvError(
            f"Could not safely open node.env: {path}: {error}"
        ) from error
    try:
        with os.fdopen(file_descriptor, "rb") as file:
            data = file.read()
    except OSError as error:
        raise NodeEnvError(f"Could not read node.env: {path}: {error}") from error
    try:
        return data.decode("utf-8")
    except UnicodeDecodeError as error:
        raise NodeEnvError(f"node.env is not valid UTF-8: {path}") from error


def load(path: Path = DEFAULT_PATH) -> NodeEnv:
    """Read, parse, and validate node.env."""
    try:
        text = read_text(path)
    except NodeEnvError:
        raise
    except NodeError as error:
        raise NodeEnvError(str(error)) from error
    return validate(parse(text), source=path)


# ---------------------------------------------------------------------------
# Command line
# ---------------------------------------------------------------------------


def shell_exports(env: NodeEnv) -> str:
    """Render the validated settings as ``export KEY='value'`` lines for bash.

    The installer is written in bash, which parses ``KEY=value`` files with
    its own grammar: ``$`` expands, a mid-line ``#`` starts a comment, and
    anything that is not an assignment is executed. Sourcing node.env would
    therefore give the installer different values from the ones this parser
    validated and the units will read. Every value is single-quoted, so bash
    reproduces it byte for byte and cannot expand or execute anything in it.

    Every key present in the file is exported, as ``source`` would have done,
    with MONITORING_MODE already resolved to the role's default when blank.
    """
    import shlex

    return "".join(
        f"export {key}={shlex.quote(value)}\n" for key, value in env.raw.items()
    )


def main(argv: list[str] | None = None) -> int:
    """``python3 -m seismic_node.nodeenv [--check|--export] [PATH]``."""
    import argparse
    import sys

    parser = argparse.ArgumentParser(
        prog="seismic_node.nodeenv",
        description="Validate a node.env file the way systemd and seismic-node read it.",
    )
    parser.add_argument("path", nargs="?", type=Path, default=DEFAULT_PATH)
    action = parser.add_mutually_exclusive_group()
    action.add_argument(
        "--check",
        action="store_true",
        help="validate and exit 0 on success (the default action)",
    )
    action.add_argument(
        "--export",
        action="store_true",
        help="validate, then print the settings as bash export statements",
    )
    parser.add_argument(
        "--no-ownership",
        action="store_true",
        help="skip root-ownership checks (for files staged before installation)",
    )
    args = parser.parse_args(argv)
    try:
        if args.no_ownership:
            env = validate(
                parse(args.path.read_text(encoding="utf-8")), source=args.path
            )
        else:
            env = load(args.path)
    except (NodeError, OSError) as error:
        print(f"node.env: {error}", file=sys.stderr)
        return 1
    if args.export:
        sys.stdout.write(shell_exports(env))
        return 0
    applicable = sum(1 for key in SCHEMA if env.applies(key))
    print(f"{args.path}: valid {env.role} configuration ({applicable} keys apply)")
    return 0


if __name__ == "__main__":
    raise SystemExit(main())

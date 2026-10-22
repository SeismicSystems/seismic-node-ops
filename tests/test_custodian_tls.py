"""Tests for the managed OpenResty deployment driven by TLS_MODE.

The rendering functions in install/lib/openresty.sh are exercised through the
installer's step_tls mapping with every host-affecting command intercepted, so
nothing here installs packages, writes outside a temporary directory, or
touches services.
"""

from __future__ import annotations

import os
import subprocess
import tempfile
import unittest
from pathlib import Path

ROOT = Path(__file__).resolve().parents[1]

# Everything the installer would have sourced and defined before step_tls.
PRELUDE = """set -euo pipefail
SCRIPT_DIR="$ROOT/install"
REPO_ROOT="$ROOT"
TEMPLATES_DIR="$SCRIPT_DIR/templates"
TOOLS_DIR="$ROOT/tools"
LOG_FILE=${LOG_FILE:-/dev/null}
NODE_ENV_PATH=/fixture/node.env
CONFIG=/fixture/node.env
source "$SCRIPT_DIR/lib/common.sh"
source "$SCRIPT_DIR/lib/binaries.sh"
source "$SCRIPT_DIR/lib/openresty.sh"
source "$SCRIPT_DIR/lib/steps.sh"
_out() { printf '%s\\n' "$*"; }
info() { :; }; section() { :; }; success() { :; }
warn() { printf '%s\\n' "$*" >&2; }
error() { warn "$@"; }
die() { error "$@"; exit 1; }
# node.env keys step_tls reads; tests override as needed.
DOMAIN=node.example.com
CUSTODIAN_COUNCIL_PORT=7876
CUSTODIAN_PUBLIC_URL=https://own.example.com/custodian
RATE_LIMIT_RPS=20
RATE_LIMIT_BURST=40
OPENRESTY_JWT_SECRET_PATH=/fixture/secret
# Map TLS_MODE exactly as step_tls does, without running install/deploy:
# the three effectful calls are stubbed only for the duration of step_tls.
apply_tls_mode() {
    local saved
    saved=$(declare -f install_openresty deploy_openresty_configuration print_https_activation_instructions)
    install_openresty() { :; }
    deploy_openresty_configuration() { :; }
    print_https_activation_instructions() { :; }
    step_tls
    eval "$saved"
}
"""


def shell(code: str, *args: str) -> subprocess.CompletedProcess[str]:
    return subprocess.run(
        ["bash", "-c", PRELUDE + code, "fixture", *args],
        env={**os.environ, "ROOT": str(ROOT)},
        capture_output=True,
        text=True,
        timeout=15,
        check=False,
    )


def render(mode: str, lua_dir: str = "/fixture/lua", custodian_port: int = 7876) -> str:
    """Render nginx.conf for a managed TLS_MODE ("custodian-only" or "full")."""
    result = shell(
        """
TLS_MODE=$1
CUSTODIAN_COUNCIL_PORT=$3
apply_tls_mode
render_openresty_configuration "$2"
""",
        mode,
        lua_dir,
        str(custodian_port),
    )
    if result.returncode:
        raise AssertionError(result.stderr)
    return result.stdout


class TLSModeMappingTests(unittest.TestCase):
    def mapping(self, mode: str) -> dict[str, str]:
        result = shell(
            """
TLS_MODE=$1
apply_tls_mode
printf 'OPENRESTY_MODE=%s\\nCONFIGURE_PUBLIC_ENDPOINT=%s\\nEXPOSE_CUSTODIAN=%s\\nCOUNCIL_LISTEN=%s\\nCUSTODIAN_BASE_URL=%s\\n' \\
    "${OPENRESTY_MODE:-}" "${CONFIGURE_PUBLIC_ENDPOINT:-}" "${EXPOSE_CUSTODIAN:-}" "${COUNCIL_LISTEN:-}" "${CUSTODIAN_BASE_URL:-}"
""",
            mode,
        )
        self.assertEqual(result.returncode, 0, result.stderr)
        return dict(
            line.split("=", 1) for line in result.stdout.splitlines() if "=" in line
        )

    def test_managed_modes_expose_custodian_on_loopback(self):
        for mode, openresty_mode in (("custodian-only", "custodian"), ("full", "full")):
            with self.subTest(mode=mode):
                values = self.mapping(mode)
                self.assertEqual(values["OPENRESTY_MODE"], openresty_mode)
                self.assertEqual(values["CONFIGURE_PUBLIC_ENDPOINT"], "true")
                self.assertEqual(values["EXPOSE_CUSTODIAN"], "true")
                self.assertEqual(values["COUNCIL_LISTEN"], "127.0.0.1:7876")
                self.assertEqual(
                    values["CUSTODIAN_BASE_URL"], "https://node.example.com/custodian"
                )

    def test_unmanaged_modes_set_nothing(self):
        for mode in ("disabled", "external"):
            with self.subTest(mode=mode):
                values = self.mapping(mode)
                self.assertEqual(values["OPENRESTY_MODE"], "")
                self.assertEqual(values["CONFIGURE_PUBLIC_ENDPOINT"], "")

    def test_unknown_mode_is_refused(self):
        result = shell("TLS_MODE=nginx; apply_tls_mode")
        self.assertEqual(result.returncode, 1)
        self.assertIn("Unknown TLS_MODE", result.stderr)

    def test_council_listen_must_be_loopback(self):
        result = shell("COUNCIL_LISTEN=0.0.0.0:7876; validate_custodian_exposure")
        self.assertNotEqual(result.returncode, 0)
        result = shell("COUNCIL_LISTEN=127.0.0.1:7876; validate_custodian_exposure")
        self.assertEqual(result.returncode, 0)


class RenderingTests(unittest.TestCase):
    def test_custodian_only_renders_custodian_route_and_nothing_else(self):
        config = render("custodian-only")
        self.assertIn("server_name node.example.com;", config)
        self.assertIn("location = /custodian/v1/council", config)
        self.assertIn("proxy_pass http://127.0.0.1:7876/v1/council;", config)
        self.assertIn("location / { return 404; }", config)
        self.assertNotIn("_PLACEHOLDER", config)
        for forbidden in (
            "jwt_auth",
            "rate_limit.lua",
            "location /rpc",
            ":8545",
            ":8546",
            ":3030",
            ":9001",
            ":9090",
            ":42069",
            "return 301",
        ):
            self.assertNotIn(forbidden, config)

    def test_full_renders_custodian_and_node_routes(self):
        config = render("full")
        self.assertIn("proxy_pass http://127.0.0.1:7876/v1/council;", config)
        self.assertIn("return 301 https://$host$request_uri;", config)
        self.assertIn("lua_shared_dict limit_req_store", config)
        for route in ("location /rpc", "jwt_auth", "rate_limit.lua"):
            self.assertIn(route, config)
        self.assertNotIn("_PLACEHOLDER", config)

    def test_custom_port_and_lua_dir(self):
        config = render("full", lua_dir="/opt/lua", custodian_port=17876)
        self.assertIn("proxy_pass http://127.0.0.1:17876/v1/council;", config)
        self.assertNotIn(":7876", config)
        self.assertIn("/opt/lua/", config)

    def test_custodian_body_limits_are_rendered(self):
        config = render("custodian-only")
        self.assertIn("client_max_body_size 64k;", config)
        # The buffer exceeds the cap so a full body never spills to disk.
        self.assertIn("client_body_buffer_size 128k;", config)
        self.assertIn("client_body_in_file_only off;", config)
        self.assertIn("proxy_request_buffering on;", config)
        self.assertIn("proxy_max_temp_file_size 0;", config)

    def test_lua_files_match_mode(self):
        for mode, expected in (
            ("custodian-only", ["custodian.lua"]),
            ("full", ["custodian.lua", "jwt_auth.lua", "rate_limit.lua"]),
        ):
            with tempfile.TemporaryDirectory() as tmp, self.subTest(mode=mode):
                result = shell(
                    'TLS_MODE=$1; apply_tls_mode; render_openresty_lua "$2"', mode, tmp
                )
                self.assertEqual(result.returncode, 0, result.stderr)
                self.assertEqual(sorted(p.name for p in Path(tmp).iterdir()), expected)


class DeploymentTests(unittest.TestCase):
    def test_managed_deployment_validates_twice_and_never_activates(self):
        for mode in ("custodian-only", "full"):
            with tempfile.TemporaryDirectory() as tmp, self.subTest(mode=mode):
                result = shell(
                    """
TLS_MODE=$1
LOG_FILE="$2/install.log"
apply_tls_mode
# Every host effect is intercepted; staging stays temporary.
systemctl() { die 'unexpected service activation'; }
install() { printf 'INSTALL %s\\n' "$*"; }
openresty() { printf 'VALIDATE %s\\n' "$*"; }
setup_openresty_jwt_secret() { printf 'JWT_SETUP\\n'; }
persist_openresty_jwt_secret_path() { printf 'JWT_PERSIST\\n'; }
deploy_openresty_configuration
""",
                    mode,
                    tmp,
                )
                self.assertEqual(result.returncode, 0, result.stderr)
                self.assertEqual("JWT_SETUP" in result.stdout, mode == "full")
                self.assertEqual("JWT_PERSIST" in result.stdout, mode == "full")
                self.assertIn("Old routes remain active", result.stderr)
                validations = (Path(tmp) / "install.log").read_text()
                self.assertEqual(validations.count("VALIDATE -t"), 2)

    def test_unmanaged_modes_touch_nothing(self):
        for mode in ("disabled", "external"):
            with self.subTest(mode=mode):
                result = shell(
                    """
TLS_MODE=$1
for cmd in apt-get install systemctl openresty install_openresty deploy_openresty_configuration; do
    eval "$cmd() { die 'forbidden operation in $1'; }"
done
step_tls
""",
                    mode,
                )
                self.assertEqual(result.returncode, 0, result.stderr)


if __name__ == "__main__":
    unittest.main()

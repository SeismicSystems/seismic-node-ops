"""Tests for the managed OpenResty deployment driven by TLS_MODE.

The rendering functions in install/lib/openresty.sh are exercised through the
installer's step_tls mapping with every host-affecting command intercepted, so
nothing here installs packages, writes outside a temporary directory, or
touches services.
"""

from __future__ import annotations

import os
import re
import subprocess
import tempfile
import unittest
from pathlib import Path
from typing import ClassVar

ROOT = Path(__file__).resolve().parents[1]

# Everything the installer would have exported and defined before step_tls.
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


def render(
    mode: str,
    lua_dir: str = "/fixture/lua",
    custodian_port: int = 7876,
    monitoring: str = "off",
    checkpointer: bool = False,
) -> str:
    """Render nginx.conf for a managed TLS_MODE ("custodian-only" or "full")."""
    result = shell(
        """
TLS_MODE=$1
CUSTODIAN_COUNCIL_PORT=$3
MONITORING_MODE=$4
CHECKPOINTER_ENABLED=$5
apply_tls_mode
render_openresty_configuration "$2"
""",
        mode,
        lua_dir,
        str(custodian_port),
        monitoring,
        "true" if checkpointer else "false",
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

    def test_full_rejects_unprovisioned_web_routes(self):
        for monitoring in ("off", "push", "pull"):
            with self.subTest(monitoring=monitoring):
                config = render("full", monitoring=monitoring)
                catch_all = config.split("location / {", 1)[1].split("}", 1)[0]
                self.assertIn("return 404;", catch_all)
                self.assertNotIn("proxy_pass", catch_all)
                for forbidden in (":3000", "location /staking", "/var/www/html"):
                    self.assertNotIn(forbidden, config)

    def test_custom_port_and_lua_dir(self):
        config = render("full", lua_dir="/opt/lua", custodian_port=17876)
        self.assertIn("proxy_pass http://127.0.0.1:17876/v1/council;", config)
        self.assertNotIn(":7876", config)
        self.assertIn("/opt/lua/", config)

    def test_metrics_routes_exist_only_in_pull_mode(self):
        for monitoring in ("off", "push"):
            with self.subTest(monitoring):
                config = render("full", monitoring=monitoring, checkpointer=True)
                self.assertNotIn("location = /prom-", config)
                self.assertNotIn(":9090", config)
                self.assertNotIn(":9001", config)
                # The checkpointer route still proxies RPC; its /metrics is closed.
                self.assertIn("proxy_pass http://127.0.0.1:42069;", config)
                guard = config.index(
                    "location = /checkpointer/metrics { access_log off; return 404; }"
                )
                self.assertLess(guard, config.index("location ~ ^/checkpointer(?:/|$)"))

        config = render("full", monitoring="pull", custodian_port=17876)
        self.assertNotIn("/checkpointer/metrics", config)
        for route, upstream in (
            ("summit", "9090/"),
            ("reth", "9001/"),
            ("custodian", "17876/metrics"),
        ):
            with self.subTest(route):
                block = config[config.index(f"location = /prom-{route} {{") :]
                block = block[: block.index("\n}\n")]
                self.assertIn("jwt_auth()", block)
                self.assertIn("rate_limit()", block)
                self.assertIn("limit_except GET { deny all; }", block)
                self.assertIn('proxy_set_header Authorization "";', block)
                self.assertIn(f"proxy_pass http://127.0.0.1:{upstream};", block)
        # Without the checkpointer there is no checkpointer route to publish.
        self.assertNotIn("/prom-checkpointer", config)
        self.assertNotIn("_PLACEHOLDER", config)

        config = render("full", monitoring="pull", checkpointer=True)
        self.assertIn("location = /prom-checkpointer {", config)
        self.assertIn("proxy_pass http://127.0.0.1:42069/metrics;", config)

    def test_custodian_only_publishes_only_metrics_in_pull_mode(self):
        # Publishing metrics does not require publishing the node's RPC:
        # custodian-only + pull is /custodian plus the /prom-* routes, nothing
        # else, with the JWT and rate-limit machinery they need.
        config = render("custodian-only", monitoring="pull", checkpointer=True)
        self.assertIn("proxy_pass http://127.0.0.1:7876/v1/council;", config)
        self.assertIn("lua_shared_dict limit_req_store", config)
        for route, upstream in (
            ("summit", "9090/"),
            ("reth", "9001/"),
            ("custodian", "7876/metrics"),
            ("checkpointer", "42069/metrics"),
        ):
            with self.subTest(route):
                block = config[config.index(f"location = /prom-{route} {{") :]
                block = block[: block.index("\n}\n")]
                self.assertIn("jwt_auth()", block)
                self.assertIn("rate_limit()", block)
                self.assertIn(f"proxy_pass http://127.0.0.1:{upstream};", block)
        # The catch-all 404 comes after the metrics routes.
        self.assertLess(
            config.index("location = /prom-summit {"),
            config.index("location / { return 404; }"),
        )
        for forbidden in (
            "location /rpc",
            "location ~ ^/checkpointer",
            "/checkpointer/metrics",
            ":8545",
            ":8546",
            ":3030",
            "return 301",
        ):
            self.assertNotIn(forbidden, config)
        self.assertNotIn("_PLACEHOLDER", config)

    def test_custodian_only_without_pull_has_no_jwt_machinery(self):
        for monitoring in ("off", "push"):
            with self.subTest(monitoring):
                config = render(
                    "custodian-only", monitoring=monitoring, checkpointer=True
                )
                self.assertNotIn("location = /prom-", config)
                self.assertNotIn("lua_shared_dict limit_req_store", config)
                self.assertNotIn("jwt_auth", config)
                self.assertNotIn(":42069", config)

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
            for monitoring in ("off", "push"):
                with self.subTest(mode=mode, monitoring=monitoring):
                    result = shell(
                        """
TLS_MODE=$1
MONITORING_MODE=$2
for cmd in apt-get install systemctl openresty install_openresty deploy_openresty_configuration setup_openresty_jwt_secret persist_openresty_jwt_secret_path; do
    eval "$cmd() { die 'forbidden operation in $1'; }"
done
step_tls
""",
                        mode,
                        monitoring,
                    )
                    self.assertEqual(result.returncode, 0, result.stderr)

    def test_external_pull_creates_the_jwt_secret_for_the_operators_proxy(self):
        """Pull mode behind the operator's own proxy still needs the secret
        generate-openresty-jwt.sh signs with; nothing else is installed."""
        result = shell(
            """
TLS_MODE=external
MONITORING_MODE=pull
OPENRESTY_JWT_SECRET_PATH=/fixture/secret
for cmd in apt-get install systemctl openresty install_openresty deploy_openresty_configuration; do
    eval "$cmd() { die 'forbidden operation in external'; }"
done
setup_openresty_jwt_secret() { printf 'JWT_SETUP\\n'; }
persist_openresty_jwt_secret_path() { printf 'JWT_PERSIST\\n'; }
step_tls
"""
        )
        self.assertEqual(result.returncode, 0, result.stderr)
        self.assertIn("JWT_SETUP\nJWT_PERSIST\n", result.stdout)
        self.assertIn("/fixture/secret", result.stdout + result.stderr)
        self.assertIn("generate-openresty-jwt.sh", result.stdout + result.stderr)


if __name__ == "__main__":
    unittest.main()


PROXY_DIR = ROOT / "install/templates/proxy"


class ReferenceProxyTests(unittest.TestCase):
    """The TLS_MODE=external references must carry the same rules as the
    managed OpenResty policy. These static checks keep the files from drifting;
    live nginx/Caddy tests run when their fixture binaries are supplied."""

    RULES: ClassVar[dict[str, list[str]]] = {
        "nginx/custodian.conf": [
            "location = /custodian/v1/council",
            "client_max_body_size 64k;",
            "client_body_buffer_size 128k;",
            "client_body_in_file_only off;",
            "proxy_request_buffering on;",
            "proxy_max_temp_file_size 0;",
            "proxy_pass_request_headers off;",
            "access_log off;",
            'add_header Cache-Control "no-store" always;',
            "return 405;",
            "return 415;",
            "return 411;",
            "proxy_pass http://127.0.0.1:CUSTODIAN_COUNCIL_PORT/v1/council;",
            "rate=5r/s;",
            "rate=20r/s;",
            "limit_conn custodian_ip_conn 4;",
            "limit_conn custodian_global_conn 16;",
            "listen 80;",
        ],
        "caddy/Caddyfile": [
            "handle /custodian/v1/council",
            "log_skip",
            'header Cache-Control "no-store"',
            "respond @not_post 405",
            "respond @not_cbor 415",
            "respond @no_length 411",
            "respond @chunked 400",
            "respond @upgrade 400",
            "max_size 65536",
            "@oversized expression int({http.request.header.Content-Length}) > 65536",
            "respond @oversized 413",
            "request_buffers 65536",
            "reverse_proxy 127.0.0.1:CUSTODIAN_COUNCIL_PORT",
            "rewrite /v1/council",
            "header_up -*",
            "header_down -Cache-Control",
            "lb_try_duration 0",
            "http://DOMAIN",
        ],
        "envoy/envoy.yaml": [
            "path: /custodian/v1/council",
            "direct_response: { status: 405 }",
            "direct_response: { status: 415 }",
            "direct_response: { status: 411 }",
            "direct_response: { status: 400 }",
            "direct_response: { status: 404 }",
            "max_request_bytes: 65536",
            "request_timeout: 5s",
            "prefix_rewrite: /v1/council",
            "host_rewrite_literal: 127.0.0.1",
            "suppress_envoy_headers: true",
            "skip_xff_append: true",
            "generate_request_id: false",
            "retry_policy: { num_retries: 0 }",
            "connect_timeout: 1s",
            "max_requests_per_connection: 1",
            "max_connections: 16",
            "max_tokens: 20",
            "tokens_per_fill: 20",
            "key: Cache-Control, value: no-store",
            "port_value: 80",
        ],
    }

    def test_references_carry_every_rule_and_both_placeholders(self):
        for name, rules in self.RULES.items():
            text = (PROXY_DIR / name).read_text()
            with self.subTest(file=name):
                for rule in rules:
                    self.assertIn(rule, text)
                self.assertIn("DOMAIN", text)
                self.assertIn("CUSTODIAN_COUNCIL_PORT", text)
                self.assertNotIn("_PLACEHOLDER", text)

    def test_envoy_reference_has_no_access_log_and_strips_client_headers(self):
        text = (PROXY_DIR / "envoy/envoy.yaml").read_text()
        self.assertNotIn("access_log", text)
        # No (de)compressor: the route takes fixed-length CBOR only, and
        # Envoy's decompressors are its recurring remote memory-safety bugs.
        self.assertNotIn("filters.http.compressor", text)
        self.assertNotIn("filters.http.decompressor", text)
        # The Lua filter keeps only the pseudo-headers, Content-Length and
        # the headers the rejection routes match on; the route then replaces
        # content-type and drops x-forwarded-proto.
        self.assertIn('["content-length"] = true', text)
        self.assertIn("headers:remove(name)", text)
        self.assertIn("request_headers_to_remove: [x-forwarded-proto]", text)
        self.assertIn("key: Content-Type, value: application/cbor", text)
        for forbidden in ("request_headers_to_add", "x-forwarded-for"):
            self.assertNotIn(forbidden, text.split("name: http\n")[1])

    def test_limits_match_the_managed_openresty_policy(self):
        def squash(text: str) -> str:
            return re.sub(r"[ \t]+", " ", text)

        managed = squash(
            (ROOT / "install/templates/openresty/custodian-location.conf").read_text()
        )
        nginx = squash((PROXY_DIR / "nginx/custodian.conf").read_text())
        for directive in (
            "client_max_body_size 64k;",
            "client_body_buffer_size 128k;",
            "client_body_timeout 5s;",
            "proxy_connect_timeout 1s;",
            "proxy_send_timeout 5s;",
            "proxy_read_timeout 5s;",
            "limit_conn custodian_ip_conn 4;",
            "limit_conn custodian_global_conn 16;",
            "limit_req zone=custodian_ip_rate burst=10 nodelay;",
            "limit_req zone=custodian_global_rate burst=20 nodelay;",
        ):
            with self.subTest(directive=directive):
                self.assertIn(directive, managed)
                self.assertIn(directive, nginx)

    @unittest.skipUnless(
        os.environ.get("CUSTODIAN_OPENRESTY_BIN"),
        "set CUSTODIAN_OPENRESTY_BIN to an nginx/openresty binary",
    )
    def test_nginx_reference_passes_configuration_test(self):
        nginx_bin = os.environ["CUSTODIAN_OPENRESTY_BIN"]
        with tempfile.TemporaryDirectory() as tmp:
            root = Path(tmp)
            (root / "logs").mkdir()
            subprocess.run(
                [
                    "openssl",
                    "req",
                    "-x509",
                    "-newkey",
                    "rsa:2048",
                    "-nodes",
                    "-keyout",
                    str(root / "key.pem"),
                    "-out",
                    str(root / "cert.pem"),
                    "-days",
                    "1",
                    "-subj",
                    "/CN=localhost",
                ],
                check=True,
                capture_output=True,
            )
            conf = (PROXY_DIR / "nginx/custodian.conf").read_text()
            conf = conf.replace("DOMAIN", "localhost").replace(
                "CUSTODIAN_COUNCIL_PORT", "7876"
            )
            conf = conf.replace(
                "listen 443 ssl;",
                f"listen 127.0.0.1:18443 ssl;\n    ssl_certificate {root}/cert.pem;\n    ssl_certificate_key {root}/key.pem;",
            ).replace("listen 80;", "listen 127.0.0.1:18080;")
            (root / "custodian.conf").write_text(conf)
            (root / "nginx.conf").write_text(
                f"worker_processes 1;\nerror_log {root}/logs/error.log warn;\npid {root}/nginx.pid;\n"
                f"events {{ worker_connections 64; }}\n"
                f"http {{ access_log {root}/logs/access.log; client_body_temp_path {root}/body;\n"
                f"proxy_temp_path {root}/proxy; include {root}/custodian.conf; }}\n"
            )
            result = subprocess.run(
                [nginx_bin, "-p", str(root), "-c", str(root / "nginx.conf"), "-t"],
                capture_output=True,
                text=True,
                check=False,
            )
            self.assertEqual(result.returncode, 0, result.stderr)
            self.assertIn("test is successful", result.stderr)

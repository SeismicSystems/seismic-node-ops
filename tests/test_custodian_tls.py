"""Installer planning/rendering tests; never deploy or start any service."""

from __future__ import annotations

import importlib.util
import os
import subprocess
import tempfile
import unittest
from pathlib import Path

ROOT = Path(__file__).resolve().parents[1]
SPEC = importlib.util.spec_from_file_location(
    "custodian_url", ROOT / "install/lib/custodian_url.py"
)
assert SPEC and SPEC.loader
URLS = importlib.util.module_from_spec(SPEC)
SPEC.loader.exec_module(URLS)


def shell(code: str, *args: str) -> subprocess.CompletedProcess[str]:
    return subprocess.run(
        [
            "bash",
            "-c",
            """set -euo pipefail
SCRIPT_DIR="$ROOT/install"
TEMPLATES_DIR="$SCRIPT_DIR/templates"
source "$SCRIPT_DIR/lib/configuration.sh"
source "$SCRIPT_DIR/lib/openresty.sh"
_out() { printf '%s\\n' "$*"; }
info() { :; }; section() { :; }; success() { :; }
warn() { printf '%s\\n' "$*" >&2; }
error() { warn "$@"; }
die() { error "$@"; exit 1; }
"""
            + code,
            "fixture",
            *args,
        ],
        env={**os.environ, "ROOT": str(ROOT)},
        capture_output=True,
        text=True,
        timeout=15,
        check=False,
    )


def render(
    mode: str,
    custodian: bool = True,
    lua_dir: str = "/fixture/lua",
    custodian_port: int = 7876,
) -> str:
    result = shell(
        """
OPENRESTY_MODE=$1
INSTALL_CUSTODIAN=$2
DOMAIN=node.example.com
COUNCIL_LISTEN=127.0.0.1:$4
render_openresty_configuration "$3"
""",
        mode,
        str(custodian).lower(),
        lua_dir,
        str(custodian_port),
    )
    if result.returncode:
        raise AssertionError(result.stderr)
    return result.stdout


class CustodianURLTests(unittest.TestCase):
    def test_valid_https_and_loopback_tunnel_urls(self):
        for url in (
            "https://node.example.com/custodian",
            "https://node.example.com:8443/prefix/custodian/",
            "https://127.0.0.1",
            "https://[::1]:443/custodian",
        ):
            with self.subTest(url=url):
                self.assertTrue(URLS.valid_url(url))
        for url in (
            "http://localhost:7876",
            "http://127.0.0.1:7876",
            "http://127.2.3.4:7876/prefix",
            "http://[::1]:7876",
        ):
            with self.subTest(url=url):
                self.assertFalse(URLS.valid_url(url))
                self.assertTrue(URLS.valid_url(url, allow_loopback_http=True))

    def test_reject_invalid_unsafe_or_remote_plaintext_urls(self):
        for url in (
            "node:7876",
            "tcp://node:7876",
            "tls://node:7876",
            "http://10.0.0.1:7876",
            "http://example.com",
            "https://user:password@node.example.com",
            "https://node.example.com?query=x",
            "https://node.example.com/#fragment",
            "https://node.example.com:0",
            "https://node.example.com:65536",
            "https://node.example.com:",
            "https://node.example.com/$(id)",
            "https://node.example.com/%(ENV_SECRET)s",
            "https://node.example.com/a;command",
            "https://node.example.com/a b",
            "https://node.example.com/../council",
            "https://node.example.com/\n",
            "https://[::1",
            "http://127.1:7876",
            "http://2130706433:7876",
        ):
            with self.subTest(url=url):
                self.assertFalse(URLS.valid_url(url, allow_loopback_http=True))


class CustodianPortTests(unittest.TestCase):
    def test_port_validation(self):
        for port in ("1", "1024", "7876", "17876", "65535"):
            with self.subTest(port=port):
                result = shell('validate_custodian_port "$1"', port)
                self.assertEqual(result.returncode, 0, result.stderr)
        for port in (
            "",
            "0",
            "65536",
            "99999999999999999999999",
            "-1",
            "+1",
            "07876",
            "0x1ec4",
            "7876 ",
            "7876\n",
            "1+1",
            "$(id)",
            "7876; exit 0",
            "abc",
            "127.0.0.1:7876",
        ):
            with self.subTest(port=port):
                self.assertNotEqual(
                    shell('validate_custodian_port "$1"', port).returncode, 0
                )

    def test_listen_validation_requires_exact_loopback_address(self):
        self.assertEqual(
            shell('validate_custodian_listen "$1"', "127.0.0.1:17876").returncode, 0
        )
        for listen in (
            "0.0.0.0:17876",
            "10.0.0.1:17876",
            "localhost:17876",
            "[::1]:17876",
            "127.0.0.1:0",
            "127.0.0.1:65536",
            "127.0.0.1:07876",
            "127.0.0.1:17876/path",
        ):
            with self.subTest(listen=listen):
                self.assertNotEqual(
                    shell('validate_custodian_listen "$1"', listen).returncode, 0
                )

    def test_prompt_retries_invalid_port(self):
        result = shell(
            """
count=0
prompt() {
    count=$((count + 1))
    if ((count == 1)); then
        printf -v "$1" '%s' 65536
    else
        printf -v "$1" '%s' 17876
    fi
}
configure_custodian_port
printf 'RESULT=%s,%s\\n' "$COUNCIL_LISTEN" "$count"
"""
        )
        self.assertEqual(result.returncode, 0, result.stderr)
        self.assertIn("RESULT=127.0.0.1:17876,2", result.stdout)
        self.assertIn("from 1 to 65535", result.stderr)

    def test_both_roles_prompt_for_default_and_custom_port(self):
        with tempfile.TemporaryDirectory() as tmp:
            for role in ("validator", "observer"):
                for selected, expected in (("", "7876"), ("17876", "17876")):
                    with self.subTest(role=role, port=selected):
                        result = shell(
                            """
NODE_ROLE=$1
PORT_CHOICE=$2
SOCKET_PATH="$3/custodian.sock"
confirm() { return 0; }
contains_unsafe_path_characters() { return 1; }
configure_directory() { printf -v "$1" '%s' "$3"; }
configure_component_installation() { printf -v "$1" '%s' deferred; }
print_component_installation() { :; }
prompt() {
    case "$1" in
        port) printf -v "$1" '%s' "${PORT_CHOICE:-$3}" ;;
        CUSTODIAN_SOCKET) printf -v "$1" '%s' "$SOCKET_PATH" ;;
        PARENT_CUSTODIAN) printf -v "$1" '%s' https://parent.example.com:8443/custodian ;;
        CUSTODIAN_CHAIN_ID) printf -v "$1" '%s' 5124 ;;
        *) printf -v "$1" '%s' "$3" ;;
    esac
}
configure_custodian
printf 'RESULT=%s\\n' "$COUNCIL_LISTEN"
""",
                            role,
                            selected,
                            tmp,
                        )
                        self.assertEqual(result.returncode, 0, result.stderr)
                        self.assertIn(f"RESULT=127.0.0.1:{expected}", result.stdout)


class CustodianTLSPlanTests(unittest.TestCase):
    def configure(
        self, enabled: bool, choice: str = "2", confirm: bool = True, port: int = 7876
    ):
        return shell(
            """
INSTALL_CUSTODIAN=$1
CHOICE=$2
CONFIRM=$3
COUNCIL_LISTEN=127.0.0.1:$4
confirm() { [[ "$CONFIRM" == true ]]; }
load_persisted_openresty_jwt_secret_path() { return 1; }
configure_file_path() { printf -v "$1" '%s' /fixture/secret; }
prompt() {
    case "$1" in
        selection) printf -v "$1" '%s' "$CHOICE" ;;
        DOMAIN) printf -v "$1" '%s' node.example.com ;;
        CUSTODIAN_BASE_URL) printf -v "$1" '%s' https://own.example.com/custodian ;;
        *) printf -v "$1" '%s' "$3" ;;
    esac
}
configure_public_endpoint
validate_https_endpoint_plan
printf 'RESULT=%s,%s,%s,%s,%s\\n' "$OPENRESTY_MODE" "$CONFIGURE_PUBLIC_ENDPOINT" "$CUSTODIAN_BASE_URL" "$DOMAIN" "${OPENRESTY_JWT_SECRET_PATH_CONFIGURED:-false}"
""",
            str(enabled).lower(),
            choice,
            str(confirm).lower(),
            str(port),
        )

    def test_three_modes_and_custodian_disabled(self):
        cases = (
            (
                True,
                "1",
                True,
                "external,false,https://own.example.com/custodian,,false",
            ),
            (
                True,
                "2",
                True,
                "custodian,true,https://node.example.com/custodian,node.example.com,false",
            ),
            (
                True,
                "3",
                True,
                "full,true,https://node.example.com/custodian,node.example.com,true",
            ),
            (False, "2", True, "full,true,,node.example.com,true"),
            (False, "2", False, "disabled,false,,,false"),
        )
        for enabled, choice, confirm, expected in cases:
            with self.subTest(enabled=enabled, choice=choice, confirm=confirm):
                result = self.configure(enabled, choice, confirm)
                self.assertEqual(result.returncode, 0, result.stderr)
                self.assertIn("RESULT=" + expected, result.stdout)

    def test_custom_port_in_all_tls_modes(self):
        for choice in ("1", "2", "3"):
            result = self.configure(True, choice, port=17876)
            self.assertEqual(result.returncode, 0, result.stderr)
            self.assertIn("127.0.0.1:17876", result.stdout)
            self.assertNotIn("127.0.0.1:7876", result.stdout)
        for mode in ("custodian", "full"):
            for port in (17876, 65535):
                config = render(mode, custodian_port=port)
                self.assertIn(f"proxy_pass http://127.0.0.1:{port}/v1/council;", config)
                self.assertNotIn("127.0.0.1:7876", config)
                self.assertNotIn("_PLACEHOLDER", config)

    def test_custom_port_in_activation_instructions(self):
        for managed in ("true", "false"):
            result = shell(
                """
INSTALL_CUSTODIAN=true
CONFIGURE_PUBLIC_ENDPOINT=$1
COUNCIL_LISTEN=127.0.0.1:17876
CUSTODIAN_BASE_URL=https://node.example.com/custodian
print_https_activation_instructions
""",
                managed,
            )
            self.assertEqual(result.returncode, 0, result.stderr)
            self.assertIn("sport = :17876", result.stdout)
            self.assertIn("127.0.0.1:17876", result.stderr)
            self.assertNotRegex(
                result.stdout + result.stderr, r"(?<![0-9])7876(?![0-9])"
            )
            if managed == "false":
                self.assertIn("proxy to 127.0.0.1:17876", result.stdout)

    def test_custodian_only_does_not_render_jwt_or_node_routes(self):
        config = render("custodian")
        self.assertIn("location = /custodian/v1/council", config)
        self.assertIn("proxy_pass http://127.0.0.1:7876/v1/council;", config)
        self.assertIn("location / { return 404; }", config)
        self.assertNotIn("_PLACEHOLDER", config)
        for forbidden in (
            "jwt_auth",
            "rate_limit.lua",
            "location /rpc",
            ":3000",
            ":8545",
            ":8546",
            ":3030",
            ":9001",
            ":9090",
            ":42069",
            "return 301",
        ):
            self.assertNotIn(forbidden, config)
        with tempfile.TemporaryDirectory() as tmp:
            result = shell(
                'INSTALL_CUSTODIAN=true; OPENRESTY_MODE=custodian; render_openresty_lua "$1"',
                tmp,
            )
            self.assertEqual(result.returncode, 0, result.stderr)
            self.assertEqual([p.name for p in Path(tmp).iterdir()], ["custodian.lua"])

    def test_full_mode_preserves_existing_routes_with_and_without_custodian(self):
        for enabled in (True, False):
            config = render("full", enabled)
            for path in (
                "rpc",
                "ws",
                "summit",
                "ops",
                "prom-summit",
                "prom-reth",
                "staking",
            ):
                self.assertIn(f"location /{path}", config)
            self.assertIn("^/checkpointer", config)
            self.assertEqual(config.count('proxy_set_header Connection "upgrade";'), 1)
            self.assertEqual(config.count("127.0.0.1:7876/v1/council"), int(enabled))
            self.assertNotIn("_PLACEHOLDER", config)

    def test_invalid_mode_or_public_backend_refused(self):
        for mode, enabled, backend in (
            ("external", "true", "127.0.0.1:7876"),
            ("custodian", "false", "127.0.0.1:7876"),
            ("full", "true", "0.0.0.0:7876"),
            ("custodian", "true", "0.0.0.0:17876"),
            ("full", "true", "127.0.0.1:0"),
            ("custodian", "true", "127.0.0.1:65536"),
            ("full", "true", "127.0.0.1:17876; return 200"),
        ):
            result = shell(
                "OPENRESTY_MODE=$1; INSTALL_CUSTODIAN=$2; COUNCIL_LISTEN=$3; DOMAIN=node.example.com; render_openresty_configuration",
                mode,
                enabled,
                backend,
            )
            self.assertNotEqual(result.returncode, 0)

    def test_package_plan_tracks_both_managed_modes(self):
        for mode in ("external", "custodian", "full"):
            result = shell(
                """
source "$SCRIPT_DIR/lib/packages.sh"
OPENRESTY_MODE=$1
CONFIGURE_PUBLIC_ENDPOINT=true
[[ "$OPENRESTY_MODE" != external ]] || CONFIGURE_PUBLIC_ENDPOINT=false
SUMMIT_INSTALL_METHOD=prebuilt
RETH_INSTALL_METHOD=prebuilt
INSTALL_CUSTODIAN=true
CUSTODIAN_INSTALL_METHOD=prebuilt
INSTALL_CHECKPOINTER=false
dpkg-query() { printf 'install ok installed'; }
collect_system_packages
printf '%s\\n' "${SYSTEM_PACKAGES[@]}"
""",
                mode,
            )
            self.assertEqual(result.returncode, 0, result.stderr)
            packages = result.stdout.splitlines()
            self.assertEqual("luarocks" in packages, mode != "external")
            self.assertEqual("gnupg" in packages, mode != "external")
            self.assertEqual("git" in packages, mode != "external")
            self.assertEqual("build-essential" in packages, mode != "external")

    def test_managed_deployment_does_not_activate_services_or_setup_unused_jwt(self):
        for mode in ("custodian", "full"):
            with tempfile.TemporaryDirectory() as tmp:
                result = shell(
                    """
CONFIGURE_PUBLIC_ENDPOINT=true
INSTALL_CUSTODIAN=true
OPENRESTY_MODE=$1
DOMAIN=node.example.com
COUNCIL_LISTEN=127.0.0.1:7876
RATE_LIMIT_RPS=20
RATE_LIMIT_BURST=40
LOG_FILE="$2/install.log"
# All installation/service effects are intercepted. Staging stays temporary.
systemctl() { die 'unexpected service activation'; }
supervisorctl() { die 'unexpected Supervisor activation'; }
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

    def test_external_mode_never_touches_openresty_or_secret(self):
        result = shell(
            """
CONFIGURE_PUBLIC_ENDPOINT=false
for cmd in apt-get install systemctl openresty setup_openresty_jwt_secret persist_openresty_jwt_secret_path; do
    eval "$cmd() { die 'forbidden service/filesystem operation'; }"
done
install_openresty
deploy_openresty_configuration
"""
        )
        self.assertEqual(result.returncode, 0, result.stderr)
        self.assertIn("Previously active routes", result.stderr)

    def test_both_installers_configure_tls_after_custodian_and_revalidate_edits(self):
        for name in ("configuration.sh", "observer-configuration.sh"):
            text = (ROOT / "install/lib" / name).read_text()
            self.assertRegex(
                text, r"\d\)\s+configure_custodian\s+configure_public_endpoint\s+;;"
            )
            self.assertIn("if ! validate_https_endpoint_plan", text)
            self.assertIn(
                "    configure_custodian\n    configure_public_endpoint", text
            )

    def render_supervisor(self, role, listen):
        return shell(
            """
source "$SCRIPT_DIR/lib/supervisor.sh"
source "$SCRIPT_DIR/lib/observer-supervisor.sh"
CUSTODIAN_TARGET_BIN=/usr/local/bin/seismic-centralized-custodian-service
CUSTODIAN_SOCKET=/tmp/custodian.sock
CUSTODIAN_DATA_DIR=/persistence/custodian
COUNCIL_LISTEN=$2
COUNCIL_ADDRESS=0xd412c5ecd343e264381ff15afc0ad78a67b79f35
CUSTODIAN_CHAIN_ID=5124
SUMMIT_KEYS_DIR=/persistence/keys/summit
SERVICE_USER=seismic
OBSERVER_INDEX=1
PARENT_CUSTODIAN=https://parent.example.com:8443/custodian
if [[ "$1" == observer ]]; then
    render_observer_custodian_supervisor_config
else
    render_custodian_supervisor_config
fi
""",
            role,
            listen,
        )

    def test_observer_and_validator_supervisor_configs(self):
        for role in ("validator", "observer"):
            for port in (7876, 17876, 65535):
                with self.subTest(role=role, port=port):
                    result = self.render_supervisor(role, f"127.0.0.1:{port}")
                    self.assertEqual(result.returncode, 0, result.stderr)
                    self.assertNotIn("_PLACEHOLDER", result.stdout)
                    self.assertIn(f"--council-listen 127.0.0.1:{port}", result.stdout)
                    self.assertIn("autostart=false", result.stdout)
                    self.assertIn("autorestart=false", result.stdout)
                    if role == "observer":
                        self.assertIn(
                            "--parent-custodian https://parent.example.com:8443/custodian",
                            result.stdout,
                        )

    def test_supervisor_rejects_public_or_invalid_backend(self):
        for role in ("validator", "observer"):
            for listen in ("0.0.0.0:17876", "127.0.0.1:0", "127.0.0.1:65536"):
                result = self.render_supervisor(role, listen)
                self.assertNotEqual(result.returncode, 0)
                self.assertIn("127.0.0.1 with a valid port", result.stderr)

    def test_cli_compatibility_rejects_old_tcp_binary(self):
        with tempfile.TemporaryDirectory() as tmp:
            binary = Path(tmp) / "custodian"
            for http in (False, True):
                binary.write_text(
                    "#!/bin/sh\nprintf '%s\\n' '--summit-key-dir --observer --parent-custodian' "
                    + (
                        "'HTTP backend listen address'"
                        if http
                        else "'Council TCP port'"
                    )
                    + "\n"
                )
                binary.chmod(0o700)
                result = shell(
                    """
source "$SCRIPT_DIR/lib/custodian.sh"
check_service_executable_security() { return 0; }
CUSTODIAN_TARGET_BIN=$1
NODE_ROLE=observer
validate_custodian_cli_support
""",
                    str(binary),
                )
                self.assertEqual(result.returncode == 0, http, result.stderr)


if __name__ == "__main__":
    unittest.main()

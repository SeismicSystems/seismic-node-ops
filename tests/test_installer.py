"""Tests for install/install.sh that never touch the host.

Only --dry-run is exercised: it validates the config, prints the plan, and
changes nothing, so every test here runs unprivileged.
"""

from __future__ import annotations

import os
import re
import subprocess
import sys
import tempfile
import unittest
from pathlib import Path

ROOT = Path(__file__).resolve().parents[1]
INSTALLER = ROOT / "install/install.sh"
EXAMPLE = ROOT / "install/templates/node.env.example"
RUNBOOK = ROOT / "docs/RUNBOOK.md"
sys.path.insert(0, str(ROOT / "tools"))

from seismic_node import nodeenv

OBSERVER = {
    "SEISMIC_ROLE": "observer",
    "OBSERVER_PARENT_NODE_PUBLIC_KEY": "ab" * 32,
    "OBSERVER_INDEX": "3",
    "OBSERVER_PUBLIC_ADDRESS": "203.0.113.10:18551",
    "OBSERVER_P2P_PORT": "18551",
    "CUSTODIAN_PARENT_URL": "https://parent.example/custodian",
}


def example(**overrides: str) -> str:
    values = nodeenv.parse(EXAMPLE.read_text())
    values["CUSTODIAN_CHAIN_ID"] = "5124"
    values["DOMAIN"] = "node.example.com"
    values["MONITORING_NODE_NAME"] = "node-1"
    values["MONITORING_REMOTE_WRITE_URL"] = "https://metrics.example/api/v1/write"
    values.update(overrides)
    return "".join(f"{k}={v}\n" for k, v in values.items())


class InstallerTests(unittest.TestCase):
    def setUp(self):
        temporary = tempfile.TemporaryDirectory()
        self.addCleanup(temporary.cleanup)
        self.root = Path(temporary.name)

    def config(self, text: str, name: str = "node.env") -> Path:
        path = self.root / name
        path.write_text(text)
        return path

    def run_installer(self, *arguments: str) -> subprocess.CompletedProcess[str]:
        return subprocess.run(
            [str(INSTALLER), *arguments],
            capture_output=True,
            text=True,
            check=False,
            timeout=60,
            env={**os.environ, "LOG_FILE": str(self.root / "install.log")},
        )

    @staticmethod
    def strip_ansi(text: str) -> str:
        return re.sub(r"\x1b\[[0-9;]*m", "", text)

    def test_dry_run_validates_and_prints_every_step_for_both_roles(self):
        for role, overrides in (("validator", {}), ("observer", OBSERVER)):
            with self.subTest(role=role):
                result = self.run_installer(
                    "--config", str(self.config(example(**overrides))), "--dry-run"
                )
                out = self.strip_ansi(result.stdout)
                self.assertEqual(result.returncode, 0, result.stderr)
                self.assertIn(f"valid {role} configuration", out)
                self.assertIn("Dry run: no changes were made.", out)
                steps = re.findall(r"\[\*\] (\S+) \(runbook ([0-9.]+)\)", out)
                self.assertEqual(
                    [name for name, _ in steps],
                    [
                        "packages",
                        "account",
                        "directories",
                        "binaries",
                        "runner",
                        "config",
                        "keys",
                        "checkpointer",
                        "units",
                        "tls",
                    ],
                )
                self.assertFalse((self.root / "install.log").exists())

    def test_plan_sections_exist_in_the_runbook(self):
        result = self.run_installer(
            "--config", str(self.config(example())), "--dry-run"
        )
        headings = set(re.findall(r"^### (3\.\d+) ", RUNBOOK.read_text(), re.MULTILINE))
        sections = re.findall(
            r"\[\*\] \S+ \(runbook ([0-9.]+)\)", self.strip_ansi(result.stdout)
        )
        self.assertTrue(sections)
        for section in sections:
            self.assertIn(section, headings)

    def test_invalid_config_fails_before_the_plan(self):
        result = self.run_installer(
            "--config", str(self.config(example(SEISMIC_ROLE="admin"))), "--dry-run"
        )
        self.assertEqual(result.returncode, 1)
        self.assertIn("SEISMIC_ROLE", result.stderr)
        self.assertNotIn("Plan", self.strip_ansi(result.stdout))

    def test_deferred_is_rejected_before_the_plan_for_each_component(self):
        for component in ("SUMMIT", "RETH", "CUSTODIAN", "CHECKPOINTER"):
            with self.subTest(component=component):
                key = f"{component}_INSTALL_METHOD"
                path = self.config(
                    example(CHECKPOINTER_ENABLED="true", **{key: "deferred"})
                )
                result = self.run_installer("--config", str(path), "--dry-run")
                self.assertEqual(result.returncode, 1, result.stdout + result.stderr)
                self.assertIn(key, result.stderr)
                self.assertIn(
                    "must be one of: download, prebuilt, source", result.stderr
                )
                self.assertNotIn("Plan", self.strip_ansi(result.stdout))
                self.assertFalse((self.root / "install.log").exists())

    def test_only_selects_steps_in_canonical_order(self):
        result = self.run_installer(
            "--config",
            str(self.config(example())),
            "--dry-run",
            "--only",
            "units,packages",
        )
        out = self.strip_ansi(result.stdout)
        self.assertEqual(result.returncode, 0, result.stderr)
        selected = re.findall(r"\[\*\] (\S+)", out)
        self.assertEqual(selected, ["packages", "units"])
        skipped = re.findall(r"\[ \] (\S+)", out)
        self.assertIn("binaries", skipped)

    def test_unknown_step_and_missing_config_are_rejected(self):
        result = self.run_installer(
            "--config", str(self.config(example())), "--dry-run", "--only", "nope"
        )
        self.assertEqual(result.returncode, 1)
        self.assertIn("Unknown step in --only: nope", result.stderr)
        result = self.run_installer("--dry-run")
        self.assertEqual(result.returncode, 1)
        self.assertIn("--config is required", result.stderr)
        result = self.run_installer("--config", str(self.root / "absent"), "--dry-run")
        self.assertEqual(result.returncode, 1)
        self.assertIn("not found", result.stderr)

    def test_installer_reads_the_parsed_values_and_never_executes_the_file(self):
        # All three lines are valid EnvironmentFile= syntax that bash would
        # mangle or execute if the installer sourced the file instead of
        # taking the values from the parser that validated them.
        marker = self.root / "executed"
        text = example(
            RETH_DATA_DIR="/persistence/reth # fast disk",
            MONITORING_NODE_NAME=f"$(touch {marker})",
        ).replace("SEISMIC_ROLE=validator\n", "SEISMIC_ROLE = validator\n")
        self.assertIn("SEISMIC_ROLE = validator", text)
        result = self.run_installer("--config", str(self.config(text)), "--dry-run")
        out = self.strip_ansi(result.stdout)
        self.assertEqual(result.returncode, 0, result.stderr)
        self.assertIn("valid validator configuration", out)
        self.assertIn("Dry run: no changes were made.", out)
        self.assertFalse(marker.exists(), "node.env was executed by the installer")

    def test_package_step_bootstraps_curl_and_gpg_before_using_them(self):
        """Download mode adds GitHub's apt repository with curl and gpg; a
        minimal image has neither, so the step must install them from
        Ubuntu's repositories first. Replayed with the real step function on
        a PATH that lacks curl, gpg, gh and apt-get; every host operation is
        intercepted and recorded in order."""
        bin_dir = self.root / "bin"
        bin_dir.mkdir()
        for tool in ("apt-get", "curl", "gpg", "dpkg", "gh"):
            stub = bin_dir / tool
            stub.write_text(
                "#!/bin/sh\n"
                '[ -e "$STUB_DIR/$(basename "$0").available" ] || exit 127\n'
                'printf "%s %s\\n" "$(basename "$0")" "$*" >> "$STUB_LOG"\n'
                'case "$(basename "$0") $1" in\n'
                '  "gpg --show-keys") echo "fpr:::::::::7F38BBB59D064DBCB3D84D725612B36462313325:" ;;\n'
                '  "dpkg --print-architecture") echo amd64 ;;\n'
                '  "apt-get install") for p; do case $p in curl) touch "$STUB_DIR/curl.available" ;; gnupg) touch "$STUB_DIR/gpg.available" ;; esac; done ;;\n'
                "esac\n"
                "exit 0\n"
            )
            stub.chmod(0o755)
        # apt-get and dpkg are always present; curl and gpg appear only once
        # the step has installed them (gnupg provides gpg).
        (bin_dir / "apt-get.available").touch()
        (bin_dir / "dpkg.available").touch()
        (bin_dir / "gh.available").touch()
        log = self.root / "ops.log"
        result = subprocess.run(
            [
                "bash",
                "-c",
                """set -euo pipefail
export PATH="$STUB_DIR:/usr/bin:/bin"
source "$ROOT/install/lib/common.sh"
source "$ROOT/install/lib/steps.sh"
LOG_FILE=/dev/null
SUMMIT_INSTALL_METHOD=download RETH_INSTALL_METHOD=download CUSTODIAN_INSTALL_METHOD=download
CHECKPOINTER_ENABLED=false TLS_MODE=disabled
ensure_directory() { :; }
install_file() { printf 'install_file %s\\n' "$2" >> "$STUB_LOG"; }
step_packages
""",
            ],
            env={
                **os.environ,
                "ROOT": str(ROOT),
                "STUB_DIR": str(bin_dir),
                "STUB_LOG": str(log),
            },
            capture_output=True,
            text=True,
            check=False,
            timeout=30,
        )
        self.assertEqual(result.returncode, 0, result.stdout + result.stderr)
        ops = log.read_text().splitlines()
        self.assertEqual(ops[0], "apt-get update")
        self.assertEqual(ops[1], "apt-get install -y -- curl ca-certificates gnupg")
        self.assertTrue(ops[2].startswith("curl "), ops)
        self.assertTrue(ops[3].startswith("gpg --show-keys"), ops)
        self.assertIn(
            "install_file /etc/apt/keyrings/githubcli-archive-keyring.gpg", ops
        )
        self.assertIn("install_file /etc/apt/sources.list.d/github-cli.list", ops)
        self.assertEqual(ops.index("apt-get update", 1), len(ops) - 3)
        self.assertTrue(
            ops[-2].startswith("apt-get install -y -- python3 curl jq"), ops
        )
        self.assertEqual(ops[-1], "gh attestation --help")

    def test_config_step_rejects_a_genesis_file_from_another_release(self):
        """Summit's eth_genesis_hash must be the genesis of the chain the
        installed Reth runs by default; a TOML and a binary from different
        releases pass every other check and then never start consensus."""
        genesis = self.root / "genesis.toml"
        genesis.write_text((ROOT / "internal_testnet_genesis.toml").read_text())
        expected = re.search(
            r'^eth_genesis_hash = "(0x[0-9a-f]{64})"', genesis.read_text(), re.MULTILINE
        ).group(1)
        fake = self.root / "seismic-reth"

        def run(reth_hash: str | None) -> subprocess.CompletedProcess[str]:
            if reth_hash is None:
                fake.unlink(missing_ok=True)
            else:
                fake.write_text(
                    f'#!/bin/sh\n[ "$1" = genesis-hash ] || exit 2\necho {reth_hash}\n'
                )
                fake.chmod(0o755)
            return subprocess.run(
                [
                    "bash",
                    "-c",
                    """set -euo pipefail
source "$ROOT/install/lib/common.sh"
source "$ROOT/install/lib/binaries.sh"
source "$ROOT/install/lib/steps.sh"
LOG_FILE=/dev/null
run_as_service_user() { "$@"; }
# Point the check at the fixture binary instead of /usr/local/bin.
eval "$(declare -f check_genesis_matches_reth | sed "s|/usr/local/bin/\\$RETH_BIN|$FAKE_RETH|")"
check_genesis_matches_reth "$1"
""",
                    "fixture",
                    str(genesis),
                ],
                env={**os.environ, "ROOT": str(ROOT), "FAKE_RETH": str(fake)},
                capture_output=True,
                text=True,
                check=False,
                timeout=30,
            )

        result = run(expected)
        self.assertEqual(result.returncode, 0, result.stdout + result.stderr)
        self.assertIn("Genesis file matches Reth's genesis", result.stdout)
        result = run(expected.upper().replace("0X", "0x"))
        self.assertEqual(result.returncode, 0, result.stdout + result.stderr)
        other = "0x" + "ab" * 32
        result = run(other)
        self.assertNotEqual(result.returncode, 0)
        self.assertIn(
            f"Genesis mismatch: {genesis} has eth_genesis_hash {expected}",
            result.stderr,
        )
        self.assertIn(f"genesis is {other}", result.stderr)
        result = run(None)
        self.assertEqual(result.returncode, 0, result.stdout + result.stderr)
        self.assertIn(
            "is not installed yet; cannot confirm", result.stdout + result.stderr
        )

    def test_config_step_resolves_the_bootnode_enode_from_its_rpc(self):
        """RETH_BOOTNODE_RPC is the operator-facing input; the installer asks
        that Reth for its enode (seismic_nodeInfo) and writes it into
        RETH_BOOTNODE and RETH_BOOTNODES_ARG of the installed node.env."""
        enode = "enode://" + "ab" * 64 + "@203.0.113.7:30303"
        bin_dir = self.root / "bin"
        bin_dir.mkdir()
        curl = bin_dir / "curl"
        source = self.root / "node.env"

        def run(
            rpc: str, response: str | None
        ) -> tuple[subprocess.CompletedProcess[str], str]:
            if response is None:
                curl.write_text("#!/bin/sh\nexit 7\n")
            else:
                curl.write_text(
                    '#!/bin/sh\nprintf "%s %s\\n" "$(basename "$0")" "$*" >> "$STUB_LOG"\n'
                    f"printf '%s' '{response}'\n"
                )
            curl.chmod(0o755)
            source.write_text(example(RETH_BOOTNODE_RPC=rpc))
            target = self.root / "installed.env"
            target.unlink(missing_ok=True)
            log = self.root / "ops.log"
            log.unlink(missing_ok=True)
            result = subprocess.run(
                [
                    "bash",
                    "-c",
                    """set -euo pipefail
export PATH="$STUB_DIR:/usr/bin:/bin"
source "$ROOT/install/lib/common.sh"
source "$ROOT/install/lib/steps.sh"
LOG_FILE=/dev/null
export RETH_BOOTNODE_RPC="$1" RETH_BOOTNODE="" RETH_BOOTNODES_ARG=""
resolve_reth_bootnode "$2" "$3"
printf 'shell: %s | %s\\n' "$RETH_BOOTNODE" "$RETH_BOOTNODES_ARG"
""",
                    "fixture",
                    rpc,
                    str(source),
                    str(target),
                ],
                env={
                    **os.environ,
                    "ROOT": str(ROOT),
                    "STUB_DIR": str(bin_dir),
                    "STUB_LOG": str(log),
                },
                capture_output=True,
                text=True,
                check=False,
                timeout=30,
            )
            return result, target.read_text() if target.exists() else ""

        rpc = "https://bootnode.example/rpc"
        result, installed = run(
            rpc, '{"jsonrpc":"2.0","id":1,"result":{"enode":"' + enode + '"}}'
        )
        self.assertEqual(result.returncode, 0, result.stdout + result.stderr)
        self.assertIn(f"RETH_BOOTNODE={enode}\n", installed)
        self.assertIn(f"RETH_BOOTNODES_ARG=--bootnodes {enode}\n", installed)
        self.assertIn(f"RETH_BOOTNODE_RPC={rpc}\n", installed)  # kept as the record
        self.assertIn(f"shell: {enode} | --bootnodes {enode}", result.stdout)
        ops = (self.root / "ops.log").read_text()
        self.assertIn(f"-X POST {rpc}", ops)
        self.assertIn('"method":"seismic_nodeInfo"', ops)
        # The result validates, and the units get a two-word --bootnodes.
        nodeenv.validate(nodeenv.parse(installed))
        # The rest of the file is untouched.
        before = [
            l
            for l in source.read_text().splitlines()
            if not l.startswith("RETH_BOOTNODE")
        ]
        after = [l for l in installed.splitlines() if not l.startswith("RETH_BOOTNODE")]
        self.assertEqual(before, after)

        for name, response, message in (
            ("no enode", '{"result":{}}', "did not return result.enode"),
            (
                "malformed",
                '{"result":{"enode":"enode://ab@h:30303"}}',
                "malformed enode",
            ),
            (
                "bad port",
                '{"result":{"enode":"enode://' + "ab" * 64 + '@h:70000"}}',
                "invalid port",
            ),
            ("unreachable", None, "Could not query the bootnode Reth RPC"),
        ):
            with self.subTest(case=name):
                result, installed = run(rpc, response)
                self.assertNotEqual(result.returncode, 0)
                self.assertIn(message, result.stderr)

        # Without the RPC key the file is copied as is and curl is never run.
        curl.write_text("#!/bin/sh\necho CURL_MUST_NOT_RUN >&2; exit 9\n")
        result, installed = run("", None)
        curl.chmod(0o755)
        source.write_text(example(RETH_BOOTNODE_RPC=""))
        result, installed = run("", "unused")
        self.assertEqual(result.returncode, 0, result.stdout + result.stderr)
        self.assertEqual(installed, source.read_text())
        self.assertFalse((self.root / "ops.log").exists())

    def test_real_run_requires_root(self):
        if os.geteuid() == 0:
            self.skipTest("running as root")
        result = self.run_installer("--config", str(self.config(example())))
        self.assertEqual(result.returncode, 1)
        self.assertIn("must be run as root", result.stderr)


if __name__ == "__main__":
    unittest.main()

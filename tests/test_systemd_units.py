"""Static checks tying the systemd unit templates to the node.env schema."""

from __future__ import annotations

import configparser
import re
import shlex
import shutil
import subprocess
import sys
import tempfile
import unittest
from pathlib import Path

ROOT = Path(__file__).resolve().parents[1]
sys.path.insert(0, str(ROOT / "tools"))

from seismic_node import nodeenv

UNITS_DIR = ROOT / "install/templates/systemd"
EXAMPLE = ROOT / "install/templates/node.env.example"
HARDENING = UNITS_DIR / "seismic-hardening.conf"

VALIDATOR_UNITS = {
    "seismic-reth",
    "seismic-summit",
    "seismic-summit-checkpoint",
    "seismic-deposit-rpc",
    "seismic-custodian",
    "seismic-checkpointer",
    "seismic-prometheus-agent",
}
OBSERVER_UNITS = {
    "seismic-reth",
    "seismic-observer",
    "seismic-observer-checkpoint",
    "seismic-observer-custodian",
    "seismic-checkpointer",
}
# Features a unit depends on; its variables need only apply when these are on.
UNIT_FEATURES = {
    "seismic-checkpointer": {"CHECKPOINTER_ENABLED": "true"},
    "seismic-prometheus-agent": {"MONITORING_MODE": "push"},
}

BRACED = re.compile(r"\$\{([A-Z0-9_]+)\}")
UNBRACED = re.compile(r"(?<!\$)\$([A-Z0-9_]+)\b")


def read_unit(name: str) -> configparser.RawConfigParser:
    parser = configparser.RawConfigParser(strict=True, delimiters=("=",))
    parser.optionxform = str  # type: ignore[assignment]
    text = (UNITS_DIR / f"{name}.service").read_text()
    # systemd joins backslash-continued lines; configparser does not.
    parser.read_string(re.sub(r"\\\n\s*", " ", text))
    return parser


def exec_start(name: str) -> str:
    return read_unit(name).get("Service", "ExecStart")


def expand(command: str, env: dict[str, str]) -> list[str]:
    """Apply systemd's ExecStart expansion: ${X} is one word, $X splits."""
    words = []
    for word in shlex.split(command):
        if UNBRACED.fullmatch(word):
            words.extend(env.get(word[1:], "").split())
            continue
        words.append(BRACED.sub(lambda m: env.get(m.group(1), ""), word))
    return words


def full_env(**overrides: str) -> dict[str, str]:
    values = nodeenv.parse(EXAMPLE.read_text())
    values["CUSTODIAN_CHAIN_ID"] = "5124"
    values["DOMAIN"] = "node.example.com"
    values["MONITORING_NODE_NAME"] = "node-1"
    values["MONITORING_REMOTE_WRITE_URL"] = "https://metrics.example/api/v1/write"
    values.update(overrides)
    return values


OBSERVER = {
    "SEISMIC_ROLE": "observer",
    "OBSERVER_PARENT_NODE_PUBLIC_KEY": "ab" * 32,
    "OBSERVER_INDEX": "3",
    "OBSERVER_PUBLIC_ADDRESS": "203.0.113.10:18551",
    "OBSERVER_P2P_PORT": "18551",
}
EVERYTHING = {
    "CUSTODIAN_PARENT_URL": "https://parent.example/custodian",
    "CHECKPOINTER_ENABLED": "true",
    "MONITORING_MODE": "push",
    "MONITORING_NODE_NAME": "node-1",
    "MONITORING_REMOTE_WRITE_URL": "https://metrics.example/api/v1/write",
    "MONITORING_OPERATOR_REMOTE_WRITE_URL": "https://prom.op.example/api/v1/write",
}


class UnitSchemaTests(unittest.TestCase):
    def test_every_unit_is_assigned_to_a_role(self):
        on_disk = {p.stem for p in UNITS_DIR.glob("*.service")}
        self.assertEqual(on_disk, VALIDATOR_UNITS | OBSERVER_UNITS)

    def test_units_reference_only_schema_keys_that_apply(self):
        for role, units in (
            ("validator", VALIDATOR_UNITS),
            ("observer", OBSERVER_UNITS),
        ):
            overrides = {**EVERYTHING, **(OBSERVER if role == "observer" else {})}
            env = nodeenv.validate(full_env(**overrides))
            for name in units:
                text = (UNITS_DIR / f"{name}.service").read_text()
                referenced = set(BRACED.findall(text)) | set(UNBRACED.findall(text))
                with self.subTest(role=role, unit=name):
                    self.assertTrue(referenced, "unit reads nothing from node.env")
                    for key in sorted(referenced):
                        self.assertIn(key, nodeenv.SCHEMA, f"{name} references {key}")
                        self.assertTrue(env.applies(key), f"{key} does not apply")

    def test_units_load_node_env_and_run_as_fixed_accounts(self):
        for name in VALIDATOR_UNITS | OBSERVER_UNITS:
            unit = read_unit(name)
            with self.subTest(unit=name):
                self.assertEqual(
                    unit.get("Service", "EnvironmentFile"), str(nodeenv.DEFAULT_PATH)
                )
                expected_user = (
                    "seismic-prometheus"
                    if name == "seismic-prometheus-agent"
                    else nodeenv.SERVICE_USER
                )
                self.assertEqual(unit.get("Service", "User"), expected_user)

    def test_exec_start_programs_are_literal_known_binaries(self):
        known = {str(p) for p in nodeenv.BINARIES.values()} | {"/usr/bin/flock"}
        for name in VALIDATOR_UNITS | OBSERVER_UNITS:
            program = shlex.split(exec_start(name))[0]
            with self.subTest(unit=name):
                self.assertFalse(program.startswith("$"), "program must be literal")
                self.assertIn(program, known)

    def test_checkpoint_units_gate_on_the_fixed_config_path(self):
        for name in ("seismic-summit-checkpoint", "seismic-observer-checkpoint"):
            unit = read_unit(name)
            with self.subTest(unit=name):
                self.assertEqual(
                    unit.get("Unit", "ConditionPathExists"),
                    str(nodeenv.CHECKPOINT_START_CONFIG),
                )
                self.assertIn(
                    f"--config {nodeenv.CHECKPOINT_START_CONFIG}", exec_start(name)
                )

    def test_summit_variants_conflict_with_each_other(self):
        validator = {
            "seismic-summit",
            "seismic-summit-checkpoint",
            "seismic-deposit-rpc",
        }
        for name in validator:
            conflicts = set(read_unit(name).get("Unit", "Conflicts").split())
            self.assertEqual(conflicts, {f"{u}.service" for u in validator - {name}})
        for name, other in (
            ("seismic-observer", "seismic-observer-checkpoint"),
            ("seismic-observer-checkpoint", "seismic-observer"),
        ):
            self.assertEqual(
                read_unit(name).get("Unit", "Conflicts"), f"{other}.service"
            )

    def test_long_running_services_restart_on_failure(self):
        # Summit journals and fsyncs its own votes before broadcasting and
        # replays them on start (Commonware Simplex), so restarting a voting
        # process cannot double-sign; every long-running unit may self-heal.
        self.assertEqual(
            read_unit("seismic-deposit-rpc").get("Service", "Restart"), "no"
        )
        for name in (VALIDATOR_UNITS | OBSERVER_UNITS) - {"seismic-deposit-rpc"}:
            with self.subTest(unit=name):
                self.assertIn(
                    read_unit(name).get("Service", "Restart"), {"on-failure", "always"}
                )

    def test_summit_restart_pulls_reth_back_up(self):
        for name in (
            "seismic-summit",
            "seismic-summit-checkpoint",
            "seismic-observer",
            "seismic-observer-checkpoint",
        ):
            unit = read_unit(name)
            with self.subTest(unit=name):
                self.assertEqual(unit.get("Unit", "Requires"), "seismic-reth.service")
                self.assertIn("seismic-reth.service", unit.get("Unit", "After").split())
                # An explicit `systemctl restart seismic-reth` must not bounce Summit.
                self.assertFalse(unit.has_option("Unit", "PartOf"))


class ExecStartExpansionTests(unittest.TestCase):
    def test_optional_flags_expand_to_nothing_when_empty(self):
        argv = expand(exec_start("seismic-reth"), full_env())
        self.assertNotIn("--bootnodes", argv)
        self.assertEqual(
            argv[argv.index("--seismic.purpose-keys-source") + 1], "custodian"
        )
        self.assertEqual(argv[argv.index("--datadir") + 1], "/persistence/reth")
        argv = expand(exec_start("seismic-summit"), full_env())
        self.assertNotIn("--bootstrappers", argv)
        self.assertEqual(
            argv[:4],
            [
                "/usr/bin/flock",
                "--nonblock",
                "--no-fork",
                "/persistence/summit/.summit-process.lock",
            ],
        )

    def test_optional_flags_expand_to_two_words_when_set(self):
        enode = "enode://ab@203.0.113.1:30303"
        env = full_env(
            RETH_BOOTNODE=enode,
            RETH_BOOTNODES_ARG=f"--bootnodes {enode}",
            SUMMIT_BOOTSTRAPPERS_FILE="/persistence/keys/summit/bootstrappers.toml",
            SUMMIT_BOOTSTRAPPERS_ARG="--bootstrappers /persistence/keys/summit/bootstrappers.toml",
        )
        nodeenv.validate(env)
        argv = expand(exec_start("seismic-reth"), env)
        self.assertEqual(argv[argv.index("--bootnodes") + 1], enode)
        argv = expand(exec_start("seismic-summit"), env)
        self.assertEqual(
            argv[argv.index("--bootstrappers") + 1],
            "/persistence/keys/summit/bootstrappers.toml",
        )

    def test_no_empty_words_remain_after_expansion(self):
        for role, units in (
            ("validator", VALIDATOR_UNITS),
            ("observer", OBSERVER_UNITS),
        ):
            env = full_env(**{**EVERYTHING, **(OBSERVER if role == "observer" else {})})
            nodeenv.validate(env)
            for name in units:
                with self.subTest(role=role, unit=name):
                    argv = expand(exec_start(name), env)
                    self.assertNotIn("", argv)
                    self.assertFalse(any("$" in word for word in argv), argv)

    def test_hardening_leaves_proc_mounts_visible(self):
        # Summit v0.1.0 collect_disk_stats() reads /proc/mounts and silently
        # emits no summit_disk_* gauges if that fails, which blanks the disk
        # panels and makes DiskSpaceLow unable to fire. ProcSubset=pid hides
        # every non-PID /proc entry, /proc/mounts included.
        hardening = configparser.RawConfigParser(strict=True, delimiters=("=",))
        hardening.optionxform = str  # type: ignore[assignment]
        hardening.read_string(HARDENING.read_text())
        self.assertFalse(hardening.has_option("Service", "ProcSubset"))

    def test_observer_custodian_passes_parent_and_index(self):
        env = full_env(**{**EVERYTHING, **OBSERVER})
        argv = expand(exec_start("seismic-observer-custodian"), env)
        self.assertEqual(argv[argv.index("--observer") + 1], "3")
        self.assertEqual(
            argv[argv.index("--parent-custodian") + 1],
            EVERYTHING["CUSTODIAN_PARENT_URL"],
        )
        self.assertEqual(argv[argv.index("--council-listen") + 1], "127.0.0.1:7876")


@unittest.skipUnless(shutil.which("systemd-analyze"), "systemd-analyze not available")
class SystemdAnalyzeTests(unittest.TestCase):
    def setUp(self):
        temporary = tempfile.TemporaryDirectory()
        self.addCleanup(temporary.cleanup)
        self.root = Path(temporary.name)
        system = self.root / "etc/systemd/system"
        system.mkdir(parents=True)
        for unit in UNITS_DIR.glob("*.service"):
            shutil.copy(unit, system / unit.name)
            dropin = system / f"{unit.name}.d"
            dropin.mkdir()
            shutil.copy(HARDENING, dropin / "10-hardening.conf")
        self.system = system

    def test_units_verify_without_syntax_errors(self):
        result = subprocess.run(
            [
                "systemd-analyze",
                "verify",
                "--man=no",
                *sorted(self.system.glob("*.service")),
            ],
            capture_output=True,
            text=True,
            check=False,
            timeout=60,
        )
        tolerated = re.compile(
            r"is not executable|Failed to load environment|No such file|not found"
        )
        problems = [
            line
            for line in result.stderr.splitlines()
            if line and not tolerated.search(line)
        ]
        self.assertEqual(problems, [])

    def test_units_score_ok_on_security_analysis(self):
        for unit in sorted(self.system.glob("*.service")):
            result = subprocess.run(
                [
                    "systemd-analyze",
                    "security",
                    "--offline=true",
                    f"--root={self.root}",
                    "--no-pager",
                    unit.name,
                ],
                capture_output=True,
                text=True,
                check=False,
                timeout=60,
            )
            with self.subTest(unit=unit.name):
                self.assertEqual(result.returncode, 0, result.stderr)
                match = re.search(
                    r"Overall exposure level for .*: ([0-9.]+) (\w+)", result.stdout
                )
                self.assertIsNotNone(match, result.stdout)
                self.assertLessEqual(float(match.group(1)), 3.0)
                self.assertEqual(match.group(2), "OK")


if __name__ == "__main__":
    unittest.main()

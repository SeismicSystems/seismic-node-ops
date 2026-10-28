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
# systemd expands $NAME only for valid variable names (no leading digit), so
# the $1/$2 positional parameters inside an ExecCondition shell snippet pass
# through to sh untouched.
UNBRACED = re.compile(r"(?<!\$)\$([A-Z_][A-Z0-9_]*)\b")


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

    def test_agent_unit_gates_on_the_rendered_config(self):
        """`monitoring install` renders prometheus.yml and creates the service
        account; an enabled-but-uninstalled agent must skip before user lookup
        (217/USER would loop under Restart=always), so the gate is a [Unit]
        condition on that file, not the ExecCondition which runs as the user."""
        from seismic_node import agent

        unit = read_unit("seismic-prometheus-agent")
        self.assertEqual(unit.get("Unit", "ConditionPathExists"), str(agent.CONFIG))
        self.assertIn(
            f"--config.file={agent.CONFIG}", exec_start("seismic-prometheus-agent")
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
        enode = "enode://" + "ab" * 64 + "@203.0.113.1:30303"
        env = full_env(
            RETH_PEERING="discovery",
            RETH_BOOTNODE=enode,
            RETH_PEERING_ARGS=f"{nodeenv.DISCOVERY_FLAGS} --bootnodes {enode}",
            SUMMIT_BOOTSTRAPPERS_FILE="/persistence/keys/summit/bootstrappers.toml",
            SUMMIT_BOOTSTRAPPERS_ARG="--bootstrappers /persistence/keys/summit/bootstrappers.toml",
        )
        nodeenv.validate(env)
        argv = expand(exec_start("seismic-reth"), env)
        self.assertEqual(argv[argv.index("--bootnodes") + 1], enode)
        self.assertIn("--enable-discv5-discovery", argv)
        self.assertNotIn("--trusted-only", argv)
        # Trusted mode: the same unit, a different flag group, one CSV word.
        other = "enode://" + "cd" * 64 + "@203.0.113.2:30303"
        env = full_env(
            RETH_PEERING="trusted",
            RETH_TRUSTED_PEERS_FILE="/nonexistent/trusted-peers",
            RETH_PEERING_ARGS=f"{nodeenv.TRUSTED_FLAGS} {enode},{other}",
        )
        nodeenv.validate(env)
        argv = expand(exec_start("seismic-reth"), env)
        self.assertEqual(argv[argv.index("--trusted-peers") + 1], f"{enode},{other}")
        self.assertIn("--disable-discovery", argv)
        self.assertIn("--trusted-only", argv)
        self.assertNotIn("--bootnodes", argv)
        self.assertNotIn("--enable-discv5-discovery", argv)
        env = full_env(
            SUMMIT_BOOTSTRAPPERS_FILE="/persistence/keys/summit/bootstrappers.toml",
            SUMMIT_BOOTSTRAPPERS_ARG="--bootstrappers /persistence/keys/summit/bootstrappers.toml",
        )
        nodeenv.validate(env)
        argv = expand(exec_start("seismic-summit"), env)
        self.assertEqual(
            argv[argv.index("--bootstrappers") + 1],
            "/persistence/keys/summit/bootstrappers.toml",
        )

    def test_reth_disables_file_logging_but_keeps_stdout_for_journald(self):
        # Reth v0.1.0 enables rotating file logs by default. Under the service
        # sandbox an inaccessible default log path can panic before the Engine
        # API socket is created, taking Summit down as well. Use stdout/journald
        # for both roles without provisioning app-managed log files.
        unit = read_unit("seismic-reth")
        self.assertEqual(
            unit.get("Service", "StandardOutput", fallback="journal"), "journal"
        )
        self.assertIn(
            unit.get("Service", "StandardError", fallback="inherit"),
            {"inherit", "journal"},
        )
        for role, overrides in (("validator", {}), ("observer", OBSERVER)):
            env = full_env(**{**EVERYTHING, **overrides})
            nodeenv.validate(env)
            argv = expand(exec_start("seismic-reth"), env)
            with self.subTest(role=role):
                self.assertEqual(argv.count("--log.file.max-files"), 1)
                self.assertEqual(argv[argv.index("--log.file.max-files") + 1], "0")
                for quiet in ("-q", "--quiet", "--silent"):
                    self.assertNotIn(quiet, argv)

    def test_rpc_and_metrics_listen_on_loopback_only(self):
        """OpenResty is the only public surface; the checkpointer, the managed
        proxy and the Prometheus Agent reach Reth and Summit on loopback.
        node.env has no key that could move these listeners."""
        observer = {
            **OBSERVER,
            "CUSTODIAN_PARENT_URL": EVERYTHING["CUSTODIAN_PARENT_URL"],
        }
        for role, units in (
            ("validator", ("seismic-summit", "seismic-summit-checkpoint")),
            ("observer", ("seismic-observer", "seismic-observer-checkpoint")),
        ):
            env = full_env(**(observer if role == "observer" else {}))
            nodeenv.validate(env)
            for name in units:
                with self.subTest(role=role, unit=name):
                    argv = expand(exec_start(name), env)
                    self.assertEqual(argv[argv.index("--rpc-ip") + 1], "127.0.0.1")
                    self.assertEqual(argv[argv.index("--prom-ip") + 1], "127.0.0.1")
                    self.assertEqual(
                        argv[argv.index("--prom-port") + 1],
                        str(nodeenv.SUMMIT_METRICS_PORT),
                    )
        argv = expand(exec_start("seismic-reth"), full_env())
        self.assertEqual(argv[argv.index("--http.addr") + 1], "127.0.0.1")
        self.assertEqual(argv[argv.index("--ws.addr") + 1], "127.0.0.1")
        self.assertEqual(argv[argv.index("--metrics") + 1], "9001")
        self.assertNotIn("RPC_BIND", "".join(nodeenv.SCHEMA))

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

    def test_optional_units_skip_cleanly_unless_node_env_enables_them(self):
        """Both optional units may be enabled at boot. Their ExecCondition,
        run here through /bin/sh with systemd's expansion applied, must pass
        only when node.env turns the feature on, so a unit left enabled after
        MONITORING_MODE=off/pull or CHECKPOINTER_ENABLED=false skips instead of
        resuming with retained config, token and WAL. Exit 1 is the "skip"
        range (1-254); 255+ would mark the unit failed."""
        observer = {
            **OBSERVER,
            "CUSTODIAN_PARENT_URL": EVERYTHING["CUSTODIAN_PARENT_URL"],
            "MONITORING_OPERATOR_REMOTE_WRITE_URL": EVERYTHING[
                "MONITORING_OPERATOR_REMOTE_WRITE_URL"
            ],
        }
        cases = [
            ("seismic-prometheus-agent", {}, "push", True),
            ("seismic-prometheus-agent", {}, "pull", False),
            ("seismic-prometheus-agent", {}, "off", False),
            ("seismic-prometheus-agent", {}, "", True),  # validator default
            ("seismic-prometheus-agent", observer, "push", True),
            ("seismic-prometheus-agent", observer, "off", False),
            ("seismic-prometheus-agent", observer, "", False),  # observer default
            ("seismic-checkpointer", {"CHECKPOINTER_ENABLED": "true"}, None, True),
            ("seismic-checkpointer", {"CHECKPOINTER_ENABLED": "false"}, None, False),
        ]
        for name, extra, mode, expected in cases:
            env = full_env(**extra)
            if mode is not None:
                env["MONITORING_MODE"] = mode
            nodeenv.validate(dict(env))
            argv = expand(read_unit(name).get("Service", "ExecCondition"), env)
            with self.subTest(unit=name, role=env["SEISMIC_ROLE"], mode=mode, **extra):
                self.assertEqual(argv[0], "/bin/sh")
                result = subprocess.run(
                    argv, capture_output=True, text=True, check=False, timeout=10
                )
                self.assertEqual(result.returncode, 0 if expected else 1, result)
                if not expected:
                    self.assertIn("not started", result.stdout)

    def test_observer_custodian_passes_parent_and_index(self):
        env = full_env(**{**EVERYTHING, **OBSERVER})
        argv = expand(exec_start("seismic-observer-custodian"), env)
        self.assertEqual(argv[argv.index("--observer") + 1], "3")
        self.assertEqual(
            argv[argv.index("--parent-custodian") + 1],
            EVERYTHING["CUSTODIAN_PARENT_URL"],
        )
        self.assertEqual(argv[argv.index("--council-listen") + 1], "127.0.0.1:7876")


def runbook_block(heading: str) -> str:
    """The first ```bash block under a RUNBOOK heading."""
    text = (ROOT / "docs/RUNBOOK.md").read_text()
    section = text.split(heading, 1)[1]
    return section.split("```bash\n", 1)[1].split("```", 1)[0]


def effective_service_settings(unit: Path) -> dict[str, list[str]]:
    """[Service] values of a unit with its drop-ins applied the way systemd
    does: later assignments append to list settings, and an empty assignment
    resets the list, so ``ExecStart=`` followed by ``ExecStart=...`` replaces."""
    settings: dict[str, list[str]] = {}
    for path in [unit, *sorted((unit.parent / f"{unit.name}.d").glob("*.conf"))]:
        text = re.sub(r"\\\n\s*", " ", path.read_text())
        section = None
        for line in text.splitlines():
            if line.startswith("["):
                section = line.strip()
                continue
            if section != "[Service]" or not line.strip() or line.startswith("#"):
                continue
            key, _, value = line.partition("=")
            if value:
                settings.setdefault(key, []).append(value)
            else:
                settings[key] = []
    return settings


class RunbookAccountOverrideTests(unittest.TestCase):
    """Replay runbook section 7 (running as a different account) against the
    shipped units, as the installer lays them out."""

    def override(self, units: set[str]) -> Path:
        temporary = tempfile.TemporaryDirectory()
        self.addCleanup(temporary.cleanup)
        root = Path(temporary.name)
        system = root / "etc/systemd/system"
        system.mkdir(parents=True)
        for name in units:
            shutil.copy(UNITS_DIR / f"{name}.service", system / f"{name}.service")
            dropin = system / f"{name}.service.d"
            dropin.mkdir()
            shutil.copy(HARDENING, dropin / "10-hardening.conf")
        script = runbook_block("## 7. Running as a different account").replace(
            "/etc/systemd/system", str(system)
        )
        result = subprocess.run(
            [
                "bash",
                "-c",
                "set -euo pipefail\n"
                'useradd() { printf "%s\\n" "$*" >> "$ROOT/useradd"; }\n'
                'chown() { printf "%s\\n" "$*" >> "$ROOT/chown"; }\n'
                'systemctl() { printf "%s\\n" "$*" >> "$ROOT/systemctl"; }\n' + script,
            ],
            env={"PATH": "/usr/bin:/bin", "ROOT": str(root)},
            capture_output=True,
            text=True,
            check=False,
            timeout=30,
        )
        self.assertEqual(result.returncode, 0, result.stdout + result.stderr)
        self.assertEqual((root / "systemctl").read_text(), "daemon-reload\n")
        self.assertIn("--user-group ops", (root / "useradd").read_text())
        self.assertIn("/persistence/custodian", (root / "chown").read_text())
        return system

    def test_every_node_service_runs_as_the_new_account(self):
        for role, units in (
            ("validator", VALIDATOR_UNITS),
            ("observer", OBSERVER_UNITS),
        ):
            system = self.override(units)
            for name in units:
                with self.subTest(role=role, unit=name):
                    settings = effective_service_settings(system / f"{name}.service")
                    if name == "seismic-prometheus-agent":
                        self.assertNotEqual(settings["User"][-1], "ops")
                        continue
                    self.assertEqual(settings["User"][-1], "ops")
                    self.assertEqual(settings["Group"][-1], "ops")

    def test_custodian_keeps_its_account_and_allows_the_new_one(self):
        for role, units, custodian in (
            ("validator", VALIDATOR_UNITS, "seismic-custodian"),
            ("observer", OBSERVER_UNITS, "seismic-observer-custodian"),
        ):
            with self.subTest(role=role):
                system = self.override(units)
                unit = system / f"{custodian}.service"
                original = shlex.split(exec_start(custodian))
                settings = effective_service_settings(unit)
                # User=/Group= from the loop must survive the ExecStart rewrite:
                # the data directory is chowned to the new account right after.
                self.assertEqual(settings["User"][-1], "ops")
                self.assertEqual(settings["Group"][-1], "ops")
                # Exactly one ExecStart remains, identical to the shipped one
                # except for the socket ACL naming the account Reth runs as.
                self.assertEqual(len(settings["ExecStart"]), 1)
                rewritten = shlex.split(settings["ExecStart"][0])
                self.assertEqual(rewritten[-1], "ops:tx-io,rng")
                self.assertEqual(rewritten[:-1], original[:-1])
                self.assertNotIn("seismic:tx-io,rng", rewritten)
                dropin = (unit.parent / f"{unit.name}.d/20-user.conf").read_text()
                self.assertEqual(dropin.count("[Service]"), 1)


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

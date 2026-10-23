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

    def test_real_run_requires_root(self):
        if os.geteuid() == 0:
            self.skipTest("running as root")
        result = self.run_installer("--config", str(self.config(example())))
        self.assertEqual(result.returncode, 1)
        self.assertIn("must be run as root", result.stderr)


if __name__ == "__main__":
    unittest.main()

"""Run the VM harness with fake transports, never SSH to or provision a host."""

from __future__ import annotations

import json
import os
import subprocess
import sys
import tempfile
import unittest
from pathlib import Path

ROOT = Path(__file__).resolve().parents[1]
sys.path.insert(0, str(ROOT / "tools"))

from seismic_node import nodeenv

HARNESS = ROOT / "tests/vm/run.sh"
EXAMPLE = ROOT / "install/templates/node.env.example"


class VmHarnessTests(unittest.TestCase):
    def setUp(self):
        temporary = tempfile.TemporaryDirectory(prefix="vm-harness-")
        self.addCleanup(temporary.cleanup)
        self.root = Path(temporary.name)
        self.calls = self.root / "calls.jsonl"
        self.uploaded = self.root / "uploaded.env"
        self.tmpdir = self.root / "tmp"
        self.tmpdir.mkdir()
        binary_dir = self.root / "bin"
        binary_dir.mkdir()
        stub = binary_dir / "transport"
        stub.write_text(
            f"#!{sys.executable}\n"
            "import json, os, sys\n"
            "from pathlib import Path\n"
            "name = Path(sys.argv[0]).name\n"
            "with Path(os.environ['VM_TEST_CALLS']).open('a') as log:\n"
            "    log.write(json.dumps([name, *sys.argv[1:]]) + '\\n')\n"
            "if name == 'scp':\n"
            "    Path(os.environ['VM_TEST_UPLOAD']).write_bytes(Path(sys.argv[-2]).read_bytes())\n"
        )
        stub.chmod(0o755)
        for name in ("ssh", "scp", "rsync"):
            (binary_dir / name).symlink_to(stub)
        self.environment = {
            key: value for key, value in os.environ.items() if not key.startswith("VM_")
        }
        self.environment.update(
            PATH=f"{binary_dir}{os.pathsep}{os.environ['PATH']}",
            VM_HOST="fixture@vm.invalid",
            VM_TEST_CALLS=str(self.calls),
            VM_TEST_UPLOAD=str(self.uploaded),
            TMPDIR=str(self.tmpdir),
        )

    def run_harness(self, *arguments):
        return subprocess.run(
            ["bash", str(HARNESS), *arguments],
            cwd=self.root,
            env=self.environment,
            capture_output=True,
            text=True,
            timeout=20,
            check=False,
        )

    def transport_calls(self):
        if not self.calls.exists():
            return []
        return [json.loads(line) for line in self.calls.read_text().splitlines()]

    def test_default_config_is_valid_test_only_and_temporary(self):
        result = self.run_harness("--", "--dry-run")
        self.assertEqual(result.returncode, 0, result.stdout + result.stderr)
        config = nodeenv.validate(nodeenv.parse(self.uploaded.read_text()))
        self.assertEqual(config.role, "validator")
        self.assertEqual(config.integer("CUSTODIAN_CHAIN_ID"), 5124)
        self.assertEqual(config.string("DOMAIN"), "seismic-vm.invalid")
        self.assertEqual(config.monitoring_mode, "off")
        for key in (
            "MONITORING_NODE_NAME",
            "MONITORING_REMOTE_WRITE_URL",
            "MONITORING_OPERATOR_REMOTE_WRITE_URL",
        ):
            self.assertEqual(config.raw[key], "", key)
        calls = self.transport_calls()
        self.assertEqual([call[0] for call in calls], ["rsync", "scp", "ssh"])
        self.assertIn("valid validator configuration", result.stdout)
        self.assertLess(
            result.stdout.index("valid validator"), result.stdout.index("==> syncing")
        )
        self.assertIn("--dry-run", calls[-1][-1])
        generated = Path(calls[1][-2])
        self.assertEqual(generated.parent, self.tmpdir)
        self.assertFalse(generated.exists(), "generated config was not cleaned up")

    def test_custom_config_is_validated_and_uploaded_unchanged(self):
        path = self.root / "custom config.env"
        text = EXAMPLE.read_text()
        for key, value in {
            "CUSTODIAN_CHAIN_ID": "5124",
            "DOMAIN": "validator.example.com",
            "MONITORING_MODE": "push",
            "MONITORING_NODE_NAME": "validator-01",
            "MONITORING_REMOTE_WRITE_URL": "https://metrics.example.com/api/v1/write",
        }.items():
            text = text.replace(f"{key}=\n", f"{key}={value}\n")
        path.write_text(text)
        self.environment["VM_SSH_KEY"] = str(self.root / "ssh key")
        result = self.run_harness("--config", str(path), "--", "--dry-run")
        self.assertEqual(result.returncode, 0, result.stdout + result.stderr)
        self.assertEqual(path.read_text(), text)
        self.assertEqual(self.uploaded.read_text(), text)
        self.assertIn("valid validator configuration", result.stdout)
        calls = self.transport_calls()
        self.assertEqual([call[0] for call in calls], ["rsync", "scp", "ssh"])
        self.assertEqual(calls[1][-2], str(path))
        config = nodeenv.validate(nodeenv.parse(self.uploaded.read_text()))
        self.assertEqual(config.monitoring_mode, "push")
        self.assertEqual(list(self.tmpdir.iterdir()), [])

    def test_invalid_custom_config_fails_before_any_remote_operation(self):
        path = self.root / "invalid.env"
        text = EXAMPLE.read_text().replace(
            "CUSTODIAN_CHAIN_ID=\n", "CUSTODIAN_CHAIN_ID=5124\n"
        )
        path.write_text(text)
        result = self.run_harness("--config", str(path))
        self.assertNotEqual(result.returncode, 0, result.stdout + result.stderr)
        self.assertIn("DOMAIN", result.stderr)
        self.assertEqual(self.transport_calls(), [])
        self.assertFalse(self.uploaded.exists())
        self.assertEqual(path.read_text(), text)

    def test_default_config_is_removed_if_local_validation_fails(self):
        python = self.root / "bin/python3"
        python.write_text("#!/bin/sh\nexit 42\n")
        python.chmod(0o755)
        result = self.run_harness()
        self.assertEqual(result.returncode, 42, result.stdout + result.stderr)
        self.assertEqual(self.transport_calls(), [])
        self.assertFalse(self.uploaded.exists())
        self.assertEqual(list(self.tmpdir.iterdir()), [])

    def test_missing_config_fails_before_any_remote_operation(self):
        result = self.run_harness("--config", str(self.root / "missing.env"))
        self.assertNotEqual(result.returncode, 0, result.stdout + result.stderr)
        self.assertEqual(self.transport_calls(), [])
        self.assertFalse(self.uploaded.exists())


if __name__ == "__main__":
    unittest.main()

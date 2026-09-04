"""Safety tests for the Summit checkpoint startup runner."""

from __future__ import annotations

import importlib.util
import os
import subprocess
import tempfile
import unittest
from pathlib import Path
from unittest import mock

REPOSITORY_ROOT = Path(__file__).resolve().parents[1]
RUNNER_PATH = REPOSITORY_ROOT / "tools/checkpoint-start/summit-checkpoint-runner.py"
SPEC = importlib.util.spec_from_file_location("summit_checkpoint_runner", RUNNER_PATH)
assert SPEC is not None and SPEC.loader is not None
runner = importlib.util.module_from_spec(SPEC)
SPEC.loader.exec_module(runner)


class SummitCheckpointRunnerTests(unittest.TestCase):
    def test_rejects_runner_managed_and_unsafe_options(self) -> None:
        for option in (
            "--checkpoint-path=/tmp/checkpoint",
            "--weak-subjectivity-path",
            "--checkpoint-or-default",
            "--unsafe-skip-checkpoint-verification=true",
        ):
            with (
                self.subTest(option=option),
                self.assertRaisesRegex(runner.RunnerError, "forbidden"),
            ):
                runner.validate_command(["/bin/true", option])

    def test_config_schema_is_exact(self) -> None:
        with tempfile.TemporaryDirectory() as temporary_directory:
            config = Path(temporary_directory) / "config.toml"
            config.write_text(
                'checkpoint_path = "/checkpoint"\n'
                'weak_subjectivity_path = "/anchor.toml"\n'
                'unexpected = "value"\n'
            )
            with (
                mock.patch.object(
                    runner,
                    "require_root_managed_file",
                    side_effect=runner.require_regular_file,
                ),
                self.assertRaisesRegex(runner.RunnerError, "unexpected keys"),
            ):
                runner.load_config(config)

    def test_lock_is_compatible_with_usr_bin_flock(self) -> None:
        with tempfile.TemporaryDirectory() as temporary_directory:
            lock_path = Path(temporary_directory) / "summit.lock"
            descriptor = runner.acquire_lock(lock_path)
            try:
                result = subprocess.run(
                    ["/usr/bin/flock", "--nonblock", str(lock_path), "/bin/true"],
                    check=False,
                    capture_output=True,
                )
                self.assertNotEqual(result.returncode, 0)
            finally:
                os.close(descriptor)


if __name__ == "__main__":
    unittest.main()

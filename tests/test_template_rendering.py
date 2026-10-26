"""Render installer templates with real envsubst, without installing host files."""

from __future__ import annotations

import os
import shutil
import subprocess
import tempfile
import unittest
from pathlib import Path

ROOT = Path(__file__).resolve().parents[1]


@unittest.skipUnless(shutil.which("envsubst"), "envsubst (gettext-base) is required")
class TemplateRenderingTests(unittest.TestCase):
    def setUp(self):
        temporary = tempfile.TemporaryDirectory(prefix="template-render-")
        self.addCleanup(temporary.cleanup)
        self.root = Path(temporary.name)
        self.source = self.root / "template"
        self.target = self.root / "installed"
        self.target.write_text("existing configuration\n")
        self.staging = self.root / "staging"
        self.staging.mkdir()

    def render(self, template, value="rendered", fail_envsubst=False):
        self.source.write_text(template)
        environment = {
            **os.environ,
            "TPL_TEST_KNOWN": value,
            "TMPDIR": str(self.staging),
            "LC_ALL": "C",
        }
        if fail_envsubst:
            binary_dir = self.root / "bin"
            binary_dir.mkdir()
            binary = binary_dir / "envsubst"
            binary.write_text("#!/bin/sh\nprintf 'partial output'; exit 42\n")
            binary.chmod(0o755)
            environment["PATH"] = f"{binary_dir}{os.pathsep}{os.environ['PATH']}"
        return subprocess.run(
            [
                "bash",
                "-c",
                """set -euo pipefail
source "$1/install/lib/steps.sh"
die() { printf '%s\\n' "$*" >&2; exit 1; }
# Intercept only the root-owned installation; substitution stays real.
install_file() {
    printf 'INSTALL %s %s\\n' "$3" "$4"
    cp -- "$1" "$2"
}
render_template "$2" "$3" 0644 'fixture config' TPL_TEST_KNOWN
""",
                "fixture",
                str(ROOT),
                str(self.source),
                str(self.target),
            ],
            env=environment,
            capture_output=True,
            text=True,
            check=False,
            timeout=10,
        )

    def assert_rejected(self, result, diagnostic):
        self.assertNotEqual(result.returncode, 0, result.stdout + result.stderr)
        self.assertEqual(result.stderr, diagnostic + "\n")
        self.assertNotIn("INSTALL", result.stdout)
        self.assertEqual(self.target.read_text(), "existing configuration\n")
        self.assertEqual(list(self.staging.iterdir()), [])

    def test_partial_substitution_reports_only_sorted_unique_leftovers(self):
        result = self.render(
            "${TPL_TEST_KNOWN} ${TPL_TEST_Z2} ${TPL_TEST_A} "
            "${TPL_TEST_Z2} ${tpl_test_lower}\n"
        )
        self.assert_rejected(
            result,
            "fixture config template has unresolved variables: "
            "${TPL_TEST_A} ${TPL_TEST_Z2} ${tpl_test_lower}",
        )

    def test_diagnostic_uses_rendered_values_not_the_original_source(self):
        result = self.render("value=${TPL_TEST_KNOWN}\n", value="${TPL_TEST_NESTED}")
        self.assert_rejected(
            result,
            "fixture config template has unresolved variables: ${TPL_TEST_NESTED}",
        )

    def test_success_installs_only_the_fully_rendered_file_and_cleans_staging(self):
        result = self.render("value=${TPL_TEST_KNOWN}\n")
        self.assertEqual(result.returncode, 0, result.stdout + result.stderr)
        self.assertEqual(result.stdout, "INSTALL 0644 fixture config\n")
        self.assertEqual(self.target.read_text(), "value=rendered\n")
        self.assertEqual(list(self.staging.iterdir()), [])

    def test_template_without_placeholders_succeeds(self):
        result = self.render("literal configuration\n")
        self.assertEqual(result.returncode, 0, result.stdout + result.stderr)
        self.assertEqual(self.target.read_text(), "literal configuration\n")
        self.assertEqual(list(self.staging.iterdir()), [])

    def test_envsubst_failure_preserves_target_and_cleans_partial_output(self):
        result = self.render("value=${TPL_TEST_KNOWN}\n", fail_envsubst=True)
        self.assert_rejected(result, "fixture config template rendering failed")


if __name__ == "__main__":
    unittest.main()

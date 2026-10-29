"""Bounded journal policy tests: temporary files only, never host services."""

from __future__ import annotations

import configparser
import os
import re
import shutil
import subprocess
import tempfile
import unittest
from pathlib import Path

ROOT = Path(__file__).resolve().parents[1]
NAME = "zz-seismic-retention.conf"
TEMPLATE = ROOT / "install/templates/journald" / NAME
SETTINGS = {
    "Storage": "persistent",
    "SystemMaxUse": "2G",
    "SystemKeepFree": "5G",
    "SystemMaxFileSize": "128M",
    "MaxRetentionSec": "7day",
    "ForwardToSyslog": "no",
}


def journal_settings(text: str) -> dict[str, str]:
    """Read the last assignment across concatenated [Journal] sections."""
    parser = configparser.ConfigParser(strict=False, interpolation=None)
    parser.optionxform = str
    parser.read_string(text)
    return dict(parser["Journal"])


class JournaldTests(unittest.TestCase):
    def setUp(self):
        temporary = tempfile.TemporaryDirectory()
        self.addCleanup(temporary.cleanup)
        self.root = Path(temporary.name)
        self.dropins = self.root / "dropins"
        self.target = self.dropins / NAME
        self.ops = self.root / "ops.log"
        self.text_log = self.root / "syslog"
        self.text_log.write_text("old logs must survive installation\n")

    def bash(self, script: str, **environment: str):
        return subprocess.run(
            ["bash", "-c", "set -euo pipefail\n" + script],
            env={
                **os.environ,
                "ROOT": str(ROOT),
                "FIXTURE": str(self.root),
                "OPS": str(self.ops),
                **environment,
            },
            capture_output=True,
            text=True,
            check=False,
            timeout=30,
        )

    def step(self, role: str = "validator", templates: Path | None = None):
        # Run the real step, directory helper, and install_file. Only ownership
        # changes are bypassed, with exact-argument and destination checks. Even
        # a regression cannot install into /etc or invoke a real service command.
        return self.bash(
            r"""
source "$ROOT/install/lib/common.sh"
source "$ROOT/install/lib/steps.sh"
LOG_FILE=/dev/null
TEMPLATES_DIR="$FIXTURE_TEMPLATES"
JOURNALD_DROPIN_DIR="$FIXTURE/dropins"
install() {
    if [[ $# == 9 && "${*:1:8}" == '-d -o root -g root -m 0755 --' && "$9" == "$FIXTURE/dropins" ]]; then
        printf 'directory\n' >> "$OPS"
        command install -d -m 0755 -- "$9"
    elif [[ $# == 9 && "${*:1:7}" == '-o root -g root -m 0644 --' && "$8" == "$FIXTURE_TEMPLATES/journald/zz-seismic-retention.conf" && "$9" == "$FIXTURE/dropins/zz-seismic-retention.conf" ]]; then
        printf 'file\n' >> "$OPS"
        command install -m 0644 -- "$8" "$9"
    else
        echo 'unexpected install operation' >&2; exit 91
    fi
}
chown() {
    [[ $# == 3 && "$1" == root:root && "$2" == -- && "$3" == "$FIXTURE/dropins/zz-seismic-retention.conf" ]] || exit 92
}
systemctl() { echo 'unexpected service operation' >&2; exit 93; }
systemd-analyze() { echo 'unexpected host configuration access' >&2; exit 94; }
journalctl() { echo 'unexpected journal operation' >&2; exit 95; }
rm() { echo 'unexpected deletion' >&2; exit 96; }
step_logging
""",
            SEISMIC_ROLE=role,
            FIXTURE_TEMPLATES=str(templates or ROOT / "install/templates"),
        )

    def test_exact_shared_policy_and_vendor_ordering_name(self):
        self.assertEqual(journal_settings(TEMPLATE.read_text()), SETTINGS)
        self.assertGreater(NAME, "syslog.conf")
        self.assertNotIn("LogNamespace", TEMPLATE.read_text())

    def test_step_installs_for_both_roles_without_activation_or_cleanup(self):
        for role in ("validator", "observer"):
            with self.subTest(role=role):
                result = self.step(role)
                self.assertEqual(result.returncode, 0, result.stdout + result.stderr)
                self.assertEqual(self.target.read_bytes(), TEMPLATE.read_bytes())
                self.assertEqual(self.target.stat().st_mode & 0o777, 0o644)
                self.assertEqual(self.dropins.stat().st_mode & 0o777, 0o755)
                self.assertIn("Host-wide policy", result.stdout + result.stderr)
                self.assertIn("not restarted", result.stdout + result.stderr)
                self.assertEqual(
                    self.text_log.read_text(), "old logs must survive installation\n"
                )
        self.assertEqual(
            self.ops.read_text().splitlines(), ["directory", "file", "directory"]
        )

    def test_rerun_is_content_idempotent_and_updates_old_policy(self):
        first = self.step()
        self.assertEqual(first.returncode, 0, first.stdout + first.stderr)
        os.utime(self.target, ns=(1_000_000_000, 1_000_000_000))
        mtime = self.target.stat().st_mtime_ns
        rerun = self.step()
        self.assertEqual(rerun.returncode, 0, rerun.stdout + rerun.stderr)
        self.assertIn("unchanged", rerun.stdout)
        self.assertEqual(self.target.stat().st_mtime_ns, mtime)
        self.target.write_text("[Journal]\nSystemMaxUse=8G\nForwardToSyslog=yes\n")
        update = self.step()
        self.assertEqual(update.returncode, 0, update.stdout + update.stderr)
        self.assertIn("updated", update.stdout)
        self.assertEqual(self.target.read_bytes(), TEMPLATE.read_bytes())

    def test_symlink_directory_and_target_are_rejected(self):
        outside = self.root / "outside"
        outside.mkdir()
        self.dropins.symlink_to(outside, target_is_directory=True)
        result = self.step()
        self.assertNotEqual(result.returncode, 0)
        self.assertIn("symbolic link", result.stderr)
        self.assertEqual(list(outside.iterdir()), [])
        self.dropins.unlink()
        self.dropins.mkdir()
        victim = outside / "policy"
        victim.write_text("preserve me")
        self.target.symlink_to(victim)
        result = self.step()
        self.assertNotEqual(result.returncode, 0)
        self.assertIn("symbolic link", result.stderr)
        self.assertEqual(victim.read_text(), "preserve me")

    def test_missing_template_does_not_replace_existing_policy(self):
        self.dropins.mkdir()
        self.target.write_text("existing policy")
        result = self.step(templates=self.root / "missing")
        self.assertNotEqual(result.returncode, 0)
        self.assertIn("source is missing", result.stderr)
        self.assertEqual(self.target.read_text(), "existing policy")

    @unittest.skipUnless(shutil.which("systemd-analyze"), "needs systemd-analyze")
    def test_real_systemd_merge_overrides_ubuntu_syslog_forwarding(self):
        root = self.root / "host"
        vendor = root / "usr/lib/systemd/journald.conf.d"
        local = root / "etc/systemd/journald.conf.d"
        vendor.mkdir(parents=True)
        local.mkdir(parents=True)
        (root / "etc/systemd/journald.conf").write_text("[Journal]\n")
        (vendor / "syslog.conf").write_text("[Journal]\nForwardToSyslog=yes\n")
        target = local / NAME
        shutil.copyfile(TEMPLATE, target)

        def merged():
            result = subprocess.run(
                [
                    "systemd-analyze",
                    "--root",
                    str(root),
                    "cat-config",
                    "systemd/journald.conf",
                ],
                capture_output=True,
                text=True,
                check=False,
                timeout=15,
            )
            self.assertEqual(result.returncode, 0, result.stdout + result.stderr)
            return journal_settings(result.stdout)

        self.assertEqual(merged(), SETTINGS)
        # Negative control: a numeric prefix silently loses to vendor syslog.conf.
        target.rename(local / "60-retention.conf")
        self.assertEqual(merged()["ForwardToSyslog"], "yes")

    def test_runbook_policy_and_actual_manual_install_and_activation_blocks(self):
        section = (
            (ROOT / "docs/RUNBOOK.md")
            .read_text()
            .split("### 3.13 Bounded system logging", 1)[1]
            .split("### 3.14", 1)[0]
        )
        policy = re.search(r"```ini\n(.*?)```", section, re.DOTALL)
        self.assertIsNotNone(policy)
        self.assertEqual(journal_settings(policy[1]), SETTINGS)
        blocks = re.findall(r"```bash\n(.*?)```", section, re.DOTALL)
        self.assertEqual(len(blocks), 2)
        # Execute the exact documentation, mapping each privileged operation to
        # a fixture or a recorded no-op; reject every other operation.
        wrappers = r"""
REPO="$ROOT"
sudo() {
    if [[ "$*" == 'install -d -o root -g root -m 0755 /etc/systemd/journald.conf.d' ]]; then
        command install -d -m 0755 "$FIXTURE/dropins"
    elif [[ $# == 9 && "${*:1:7}" == 'install -o root -g root -m 0644' && "$8" == "$ROOT/install/templates/journald/zz-seismic-retention.conf" && "$9" == /etc/systemd/journald.conf.d/zz-seismic-retention.conf ]]; then
        command install -m 0644 "$8" "$FIXTURE/dropins/zz-seismic-retention.conf"
    elif [[ "$*" == 'systemd-analyze cat-config systemd/journald.conf' || "$*" == 'systemctl restart systemd-journald' || "$*" == 'systemctl is-active systemd-journald' || "$*" == 'journalctl -u systemd-journald --since -5min --no-pager' || "$*" == 'journalctl --disk-usage' ]]; then
        printf '%s\n' "$*" >> "$OPS"
    else
        echo 'unexpected privileged operation' >&2; exit 91
    fi
}
"""
        result = self.bash(wrappers + "\n" + "\n".join(blocks))
        self.assertEqual(result.returncode, 0, result.stdout + result.stderr)
        self.assertEqual(self.target.read_bytes(), TEMPLATE.read_bytes())
        self.assertEqual(
            self.ops.read_text().splitlines(),
            [
                "systemd-analyze cat-config systemd/journald.conf",
                "systemctl restart systemd-journald",
                "systemctl is-active systemd-journald",
                "journalctl -u systemd-journald --since -5min --no-pager",
                "journalctl --disk-usage",
            ],
        )
        self.assertEqual(
            self.text_log.read_text(), "old logs must survive installation\n"
        )

    def test_installer_reminder_is_limited_to_selected_logging_step(self):
        text = (ROOT / "install/install.sh").read_text()
        function = (
            "print_next_steps() {"
            + text.split("print_next_steps() {", 1)[1].split("\n}\n", 1)[0]
            + "\n}\n"
        )
        for selected, only in (("true", ""), ("true", "logging"), ("false", "units")):
            with self.subTest(selected=selected, only=only):
                result = self.bash(
                    'source "$ROOT/install/lib/common.sh"\n'
                    "LOG_FILE=/dev/null; TOOLS_DIR=/fixture/tools\n"
                    "SEISMIC_ROLE=observer MONITORING_MODE=off\n"
                    'step_selected() { [[ "$1" == logging && "$SELECTED" == true ]]; }\n'
                    + function
                    + "print_next_steps\n",
                    SELECTED=selected,
                    ONLY=only,
                )
                self.assertEqual(result.returncode, 0, result.stdout + result.stderr)
                self.assertEqual(
                    "sudo systemctl restart systemd-journald" in result.stdout,
                    selected == "true",
                )
                self.assertEqual(
                    "Then start the node" in result.stdout, only != "logging"
                )
                if only == "logging":
                    self.assertNotIn("observer start", result.stdout)
                    self.assertIn(
                        "node service state was left untouched", result.stdout
                    )


if __name__ == "__main__":
    unittest.main()

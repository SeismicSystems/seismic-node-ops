"""Monitoring-only installation stays inside isolated fixtures, never live nodes."""

from __future__ import annotations

import contextlib
import io
import json
import os
import stat
import subprocess
import sys
import tempfile
import tomllib
import unittest
from pathlib import Path
from types import SimpleNamespace
from unittest import mock

ROOT = Path(__file__).resolve().parents[1]
sys.path.insert(0, str(ROOT / "install/lib"))
import monitoring_only as only

agent = only.agent


def inventory_text(root, role="validator"):
    text = (
        "# Preserve this node's original inventory and formatting.\n"
        "schema_version = 1\n\n"
        f'reth_data_dir = "{root}/node/reth"\n'
        f'reth_p2p_key_path = "{root}/keys/reth/p2p-key"\n'
        f'summit_data_dir = "{root}/node/summit"\n'
        f'summit_keys_dir = "{root}/keys/summit"\n'
    )
    if role == "observer":
        text += (
            f'observer_parent_node_public_key = "0x{"ab" * 32}"\nobserver_index = 7\n'
        )
    return text


class MonitoringOnlyTests(unittest.TestCase):
    @contextlib.contextmanager
    def fixture(self, role="validator"):
        with tempfile.TemporaryDirectory() as tmp, contextlib.ExitStack() as stack:
            root = Path(tmp)
            paths = {
                "HOME": root / "agent",
                "SETTINGS": root / "agent/installation.json",
                "TOKEN": root / "agent/token",
                "CONFIG": root / "agent/prometheus.yml",
                "SUPERVISOR_CONFIG": root / "agent-supervisor.conf",
                "BIN_DIR": root / "bin/prometheus",
                "LOG_DIR": root / "agent-logs",
            }
            for name, path in paths.items():
                stack.enter_context(mock.patch.object(agent, name, path))
            stack.enter_context(
                mock.patch.object(only, "LOCK", root / "lock/install.lock")
            )
            stack.enter_context(mock.patch.object(agent, "safe_path"))

            def root_file(path):
                meta = path.lstat()
                if not stat.S_ISREG(meta.st_mode) or meta.st_mode & 0o022:
                    raise agent.AgentError("Unsafe fixture file")
                return meta

            stack.enter_context(
                mock.patch.object(agent, "root_file", side_effect=root_file)
            )
            stack.enter_context(mock.patch.object(agent.os, "chown"))
            stack.enter_context(mock.patch.object(agent.os, "fchown"))

            def directory(path, uid=0, gid=0, mode=0o755):
                path.mkdir(parents=True, exist_ok=True)
                path.chmod(mode)

            stack.enter_context(
                mock.patch.object(agent, "directory", side_effect=directory)
            )
            stack.enter_context(
                mock.patch.object(
                    agent,
                    "service_account",
                    return_value=SimpleNamespace(
                        pw_uid=os.getuid(), pw_gid=os.getgid()
                    ),
                )
            )
            stack.enter_context(mock.patch.object(agent, "install_release"))
            state = stack.enter_context(
                mock.patch.object(agent, "supervisor_state", return_value="MISSING")
            )
            run = stack.enter_context(
                mock.patch.object(
                    agent.subprocess, "run", return_value=SimpleNamespace(returncode=0)
                )
            )
            inventory = root / "inventory.toml"
            inventory.write_text(inventory_text(root, role))
            inventory.chmod(0o640)
            token = root / "handoff"
            token.write_text("a" * 64)
            token.chmod(0o600)
            # Sentinel contents are never needed by monitoring-only installation.
            for name in (
                "node/reth/data",
                "node/summit/data",
                "keys/reth/p2p-key",
                "keys/summit/node_key.pem",
                "keys/summit/consensus_key.pem",
                "node-supervisor.conf",
                "openresty.conf",
                "summit-binary",
                "reth-binary",
            ):
                path = root / name
                path.parent.mkdir(parents=True, exist_ok=True)
                path.write_text("unchanged node resource: " + name)
            with contextlib.redirect_stdout(io.StringIO()):
                yield SimpleNamespace(
                    root=root,
                    inventory=inventory,
                    token=token,
                    state=state,
                    run=run,
                    role=role,
                )

    def execute(self, fixture, answers):
        with mock.patch("builtins.input", side_effect=answers):
            only.configure(fixture.inventory, fixture.role)

    def install(self, fixture):
        self.execute(
            fixture,
            [
                "yes",
                "node.example",
                "https://metrics.example/api/v1/write",
                str(fixture.root / "wal"),
                str(fixture.token),
                "yes",
            ],
        )

    def test_install_for_both_roles_changes_only_agent_and_monitoring_table(self):
        for role in ("validator", "observer"):
            with self.subTest(role=role), self.fixture(role) as f:
                original = f.inventory.read_bytes()
                protected = {
                    p: p.read_bytes()
                    for p in f.root.rglob("*")
                    if p.is_file() and p not in (f.inventory, f.token)
                }
                self.install(f)
                updated = tomllib.loads(f.inventory.read_text())
                self.assertTrue(f.inventory.read_bytes().startswith(original))
                self.assertEqual(updated.pop("monitoring"), agent.load_settings())
                self.assertEqual(updated, tomllib.loads(original.decode()))
                self.assertEqual({p: p.read_bytes() for p in protected}, protected)
                self.assertEqual(f.inventory.stat().st_mode & 0o777, 0o640)
                self.assertNotIn("a" * 64, f.inventory.read_text())
                self.assertEqual(agent.TOKEN.stat().st_mode & 0o777, 0o640)
                self.assertTrue(
                    all(
                        call.args[0][1:4] == ["check", "config", "--agent"]
                        for call in f.run.call_args_list
                    )
                )

    def test_decline_or_cancel_is_noop(self):
        for answers in (
            ["no"],
            [
                "yes",
                "node",
                "https://metrics.example/api/v1/write",
                "WAL",
                "TOKEN",
                "no",
            ],
        ):
            with self.subTest(answers=answers), self.fixture() as f:
                original = f.inventory.read_bytes()
                self.execute(
                    f,
                    [
                        a.replace("WAL", str(f.root / "wal")).replace(
                            "TOKEN", str(f.token)
                        )
                        for a in answers
                    ],
                )
                self.assertEqual(f.inventory.read_bytes(), original)
                self.assertFalse(agent.SETTINGS.exists())
                f.run.assert_not_called()

    def test_keep_running_agent_does_not_probe_or_control_supervisor(self):
        with self.fixture() as f:
            self.install(f)
            original = f.inventory.read_bytes()
            files = {
                p: p.read_bytes()
                for p in (
                    agent.SETTINGS,
                    agent.TOKEN,
                    agent.CONFIG,
                    agent.SUPERVISOR_CONFIG,
                )
            }
            f.state.reset_mock()
            f.run.reset_mock()
            f.state.side_effect = AssertionError("keep must not inspect Supervisor")
            self.execute(f, [""])
            self.assertEqual(f.inventory.read_bytes(), original)
            self.assertEqual({p: p.read_bytes() for p in files}, files)
            f.run.assert_not_called()
            f.state.assert_not_called()

    def test_update_and_token_rotation_preserve_buffered_wal(self):
        with self.fixture() as f:
            self.install(f)
            (f.root / "wal/buffer").write_text("pending samples")
            f.token.write_text("b" * 64)
            f.state.return_value = "STOPPED"
            self.execute(f, ["update", "", "", "", str(f.token), "yes"])
            self.assertEqual(agent.TOKEN.read_text(), "b" * 64 + "\n")
            self.assertEqual((f.root / "wal/buffer").read_text(), "pending samples")
            self.assertEqual(f.inventory.read_text().count("[monitoring]"), 1)

    def test_update_requires_stopped_agent_but_not_stopped_node(self):
        with self.fixture() as f:
            self.install(f)
            original = f.inventory.read_bytes()
            f.run.reset_mock()
            for state in ("RUNNING", "EXITED", "STARTING", "BACKOFF"):
                with self.subTest(state=state):
                    f.state.return_value = state
                    with self.assertRaisesRegex(
                        agent.AgentError, "Stop prometheus-agent"
                    ):
                        self.execute(f, ["update"])
                    self.assertEqual(f.inventory.read_bytes(), original)
            f.run.assert_not_called()

    def test_disable_stops_only_agent_and_preserves_credentials_and_wal(self):
        with self.fixture("observer") as f:
            self.install(f)
            (f.root / "wal/buffer").write_text("pending")
            token = agent.TOKEN.read_bytes()
            f.run.reset_mock()
            f.state.side_effect = ["RUNNING", "STOPPED", "STOPPED"]
            self.execute(f, ["disable", "yes"])
            f.run.assert_called_once()
            self.assertEqual(
                f.run.call_args.args[0],
                ["/usr/bin/supervisorctl", "stop", "prometheus-agent"],
            )
            self.assertFalse(
                tomllib.loads(f.inventory.read_text())["monitoring"]["enabled"]
            )
            self.assertFalse(agent.load_settings()["enabled"])
            self.assertFalse(agent.SUPERVISOR_CONFIG.exists())
            self.assertEqual(agent.TOKEN.read_bytes(), token)
            self.assertEqual((f.root / "wal/buffer").read_text(), "pending")

    def test_disable_failure_keeps_inventory_unchanged(self):
        with self.fixture() as f:
            self.install(f)
            original = f.inventory.read_bytes()
            f.state.return_value = "RUNNING"
            f.run.return_value.returncode = 1
            with self.assertRaises(agent.AgentError):
                self.execute(f, ["disable", "yes"])
            self.assertEqual(f.inventory.read_bytes(), original)
            self.assertTrue(agent.load_settings()["enabled"])

    def test_install_failure_never_enables_inventory(self):
        with self.fixture() as f:
            original = f.inventory.read_bytes()
            with (
                mock.patch.object(
                    agent,
                    "install_release",
                    side_effect=agent.AgentError("download failed"),
                ),
                self.assertRaisesRegex(agent.AgentError, "download failed"),
            ):
                self.install(f)
            self.assertEqual(f.inventory.read_bytes(), original)

    def test_missing_or_wrong_role_inventory_fails_before_mutations(self):
        with self.fixture() as f:
            for role in ("observer",):
                with self.assertRaises(agent.AgentError):
                    only.read_inventory(f.inventory, role)
            f.inventory.unlink()
            with self.assertRaisesRegex(agent.AgentError, "inventory is required"):
                self.install(f)
            self.assertFalse(agent.HOME.exists())
            f.run.assert_not_called()
        with self.fixture("observer") as f, self.assertRaises(agent.AgentError):
            only.read_inventory(f.inventory, "validator")

    def test_unsafe_inventory_and_invalid_fields_are_refused(self):
        with self.fixture() as f:
            original = f.inventory.read_text()
            for contents in (
                original.replace("schema_version = 1", "schema_version = true"),
                original.replace("schema_version = 1", "schema_version = 2"),
                original + 'unexpected = "value"\n',
                original.replace(str(f.root / "node/reth"), str(f.root / "node")),
                original + '\n[monitoring]\nenabled = true\nrole = "observer"\n',
            ):
                with self.subTest(contents=contents):
                    f.inventory.write_text(contents)
                    with self.assertRaises(agent.AgentError):
                        self.install(f)
            f.inventory.write_text(original)
            f.inventory.chmod(0o666)
            with self.assertRaises(agent.AgentError):
                self.install(f)
            f.inventory.unlink()
            f.inventory.symlink_to(f.token)
            with self.assertRaises(agent.AgentError):
                self.install(f)
            self.assertFalse(agent.HOME.exists())
            f.run.assert_not_called()

    def test_inventory_root_ownership_checks_are_used_in_production(self):
        with tempfile.TemporaryDirectory() as tmp:
            path = Path(tmp) / "inventory.toml"
            path.write_text(inventory_text(Path(tmp)))
            with self.assertRaises(agent.AgentError):
                only.read_inventory(path, "validator")

    def test_existing_agent_role_mismatch_and_orphan_inventory_refused(self):
        with self.fixture() as f:
            self.install(f)
            saved = agent.load_settings()
            saved["role"] = "observer"
            agent.SETTINGS.write_text(json.dumps(saved))
            with self.assertRaisesRegex(agent.AgentError, "different node role"):
                self.execute(f, [])
            agent.SETTINGS.unlink()
            with self.assertRaisesRegex(agent.AgentError, "settings are missing"):
                self.execute(f, [])

    def test_unmanaged_loaded_agent_is_not_adopted(self):
        with self.fixture() as f:
            f.state.return_value = "STOPPED"
            with self.assertRaisesRegex(agent.AgentError, "unmanaged"):
                self.execute(f, ["yes"])
            self.assertFalse(agent.SETTINGS.exists())
            f.run.assert_not_called()

    def test_missing_supervisor_control_fails_closed(self):
        with self.fixture() as f:
            f.state.side_effect = agent.AgentError("Supervisor unavailable")
            with self.assertRaisesRegex(agent.AgentError, "unavailable"):
                self.execute(f, ["yes"])
            f.run.assert_not_called()

    def test_wal_and_token_paths_cannot_overlap_recorded_node_resources(self):
        with self.fixture() as f:
            original = f.inventory.read_bytes()
            for suffix in (
                "node/reth",
                "node/summit/nested",
                "keys/summit",
                "keys/reth",
            ):
                with self.subTest(suffix=suffix), self.assertRaises(agent.AgentError):
                    self.execute(
                        f,
                        [
                            "yes",
                            "node",
                            "https://metrics.example/api/v1/write",
                            str(f.root / suffix),
                            str(f.token),
                        ],
                    )
            with self.assertRaisesRegex(agent.AgentError, "outside node data"):
                self.execute(
                    f,
                    [
                        "yes",
                        "node",
                        "https://metrics.example/api/v1/write",
                        str(f.root / "wal"),
                        str(f.root / "keys/summit/node_key.pem"),
                    ],
                )
            self.assertEqual(f.inventory.read_bytes(), original)
            f.run.assert_not_called()

    def test_noncanonical_monitoring_table_is_not_destructively_rewritten(self):
        with self.fixture() as f:
            f.inventory.write_text(
                "monitoring = { enabled = false }\n" + f.inventory.read_text()
            )
            original = f.inventory.read_bytes()
            with (
                mock.patch.object(agent, "install") as install,
                self.assertRaisesRegex(
                    agent.AgentError, "single \\[monitoring\\] table"
                ),
            ):
                self.install(f)
            install.assert_not_called()
            self.assertEqual(f.inventory.read_bytes(), original)

    def test_inventory_changed_during_prompt_is_preserved(self):
        with self.fixture() as f:
            answers = iter(
                [
                    "yes",
                    "node",
                    "https://metrics.example/api/v1/write",
                    str(f.root / "wal"),
                    str(f.token),
                    "yes",
                ]
            )

            def answer(prompt):
                if prompt.startswith("Apply"):
                    f.inventory.write_text(
                        f.inventory.read_text() + "# external edit\n"
                    )
                return next(answers)

            with (
                mock.patch("builtins.input", side_effect=answer),
                mock.patch.object(agent, "install") as install,
                self.assertRaisesRegex(agent.AgentError, "changed concurrently"),
            ):
                only.configure(f.inventory, "validator")
            install.assert_not_called()
            self.assertTrue(f.inventory.read_text().endswith("# external edit\n"))

    def test_inventory_changed_during_install_is_not_overwritten(self):
        with self.fixture() as f:

            def external_edit(*args):
                f.inventory.write_text(f.inventory.read_text() + "# external edit\n")

            with (
                mock.patch.object(agent, "install", side_effect=external_edit),
                self.assertRaisesRegex(agent.AgentError, "changed concurrently"),
            ):
                self.install(f)
            self.assertTrue(f.inventory.read_text().endswith("# external edit\n"))
            self.assertNotIn("[monitoring]", f.inventory.read_text())

    def test_inventory_write_failure_can_be_reconciled_with_keep(self):
        with self.fixture() as f:
            original = f.inventory.read_bytes()
            atomic_write = agent.atomic_write

            def fail_inventory(path, *args, **kwargs):
                if path == f.inventory:
                    raise OSError("fixture disk failure")
                return atomic_write(path, *args, **kwargs)

            with (
                mock.patch.object(agent, "atomic_write", side_effect=fail_inventory),
                self.assertRaises(OSError),
            ):
                self.install(f)
            self.assertEqual(f.inventory.read_bytes(), original)
            self.assertTrue(agent.load_settings()["enabled"])
            f.state.reset_mock()
            f.run.reset_mock()
            self.execute(f, ["keep", "yes"])
            self.assertTrue(
                tomllib.loads(f.inventory.read_text())["monitoring"]["enabled"]
            )
            f.state.assert_not_called()
            f.run.assert_not_called()

    def test_disabled_agent_can_be_kept_then_reenabled(self):
        with self.fixture() as f:
            self.install(f)
            self.execute(f, ["disable", "yes"])
            original = f.inventory.read_bytes()
            f.run.reset_mock()
            self.execute(f, ["keep"])
            self.assertEqual(f.inventory.read_bytes(), original)
            f.run.assert_not_called()
            self.execute(f, ["update", "", "", "", "", "yes"])
            self.assertTrue(agent.load_settings()["enabled"])
            self.assertTrue(
                tomllib.loads(f.inventory.read_text())["monitoring"]["enabled"]
            )

    def test_agent_settings_changed_during_confirmation_are_not_overwritten(self):
        with self.fixture() as f:
            self.install(f)
            original = f.inventory.read_bytes()
            f.state.return_value = "STOPPED"
            answers = iter(["update", "", "", "", "", "yes"])

            def answer(prompt):
                if prompt.startswith("Apply"):
                    saved = agent.load_settings()
                    saved["node"] = "changed-externally"
                    agent.SETTINGS.write_text(json.dumps(saved))
                return next(answers)

            with (
                mock.patch("builtins.input", side_effect=answer),
                mock.patch.object(agent, "install") as install,
                self.assertRaisesRegex(agent.AgentError, "changed concurrently"),
            ):
                only.configure(f.inventory, "validator")
            install.assert_not_called()
            self.assertEqual(f.inventory.read_bytes(), original)
            self.assertEqual(agent.load_settings()["node"], "changed-externally")

    def test_agent_started_during_confirmation_prevents_update(self):
        with self.fixture() as f:
            self.install(f)
            original = f.inventory.read_bytes()
            f.state.return_value = "STOPPED"
            answers = iter(["update", "", "", "", "", "yes"])

            def answer(prompt):
                if prompt.startswith("Apply"):
                    f.state.return_value = "RUNNING"
                return next(answers)

            with (
                mock.patch("builtins.input", side_effect=answer),
                mock.patch.object(agent, "install") as install,
                self.assertRaisesRegex(agent.AgentError, "Stop prometheus-agent"),
            ):
                only.configure(f.inventory, "validator")
            install.assert_not_called()
            self.assertEqual(f.inventory.read_bytes(), original)

    def test_token_and_wal_validation_precedes_any_installation(self):
        with self.fixture() as f:
            original = f.inventory.read_bytes()
            f.token.chmod(0o644)
            with (
                mock.patch.object(agent, "install") as install,
                self.assertRaises(agent.AgentError),
            ):
                self.install(f)
            install.assert_not_called()
            self.assertEqual(f.inventory.read_bytes(), original)
            self.assertFalse(agent.HOME.exists())

    def test_second_monitoring_writer_is_refused(self):
        with self.fixture() as f, only.installation_lock():
            with self.assertRaisesRegex(agent.AgentError, "in progress"):
                self.install(f)
            self.assertFalse(agent.HOME.exists())


class EntrypointTests(unittest.TestCase):
    def run_entrypoint(self, role, args):
        # Evaluate a fixture copy with every mutating/node function replaced.
        # Never invoke the actual root installer as a test.
        source = (ROOT / f"install/install-{role}.sh").read_text()
        source = source.replace(
            'SCRIPT_DIR="$(cd "$(dirname "${BASH_SOURCE[0]}")" && pwd)"',
            f'SCRIPT_DIR="{ROOT}/install"',
        )
        self.assertTrue(source.endswith('main "$@"\n'))
        source = source.removesuffix('main "$@"\n')
        source += """
preflight() { echo PREFLIGHT; }
info() { :; }
die() { echo "$*" >&2; exit 1; }
install_monitoring_only() { printf 'AGENT %s %s\\n' "$1" "$INSTALLATION_INVENTORY_PATH"; }
"""
        forbidden = (
            "confirm_installation_inventory_overwrite configure configure_observer "
            "validate_prometheus_agent_plan install_system_packages install_openresty "
            "setup_runtime_directories setup_observer_runtime_directories install_node_binaries "
            "install_checkpointer install_custodian install_observer_custodian setup_validator_keys "
            "setup_observer_keys deploy_openresty_configuration deploy_supervisor_configuration "
            "deploy_observer_supervisor_configuration install_prometheus_agent "
            "write_validator_installation_inventory write_observer_installation_inventory "
            "print_manual_start_instructions print_observer_manual_start_instructions "
            "print_prometheus_agent_instructions"
        )
        for name in forbidden.split():
            source += f'{name}() {{ echo "FORBIDDEN {name}"; exit 99; }}\n'
        source += 'main "$@"\n'
        return subprocess.run(
            ["bash", "-s", "--", *args],
            input=source,
            text=True,
            capture_output=True,
            check=False,
            timeout=10,
        )

    def test_both_entrypoints_skip_entire_node_installation_pipeline(self):
        for role in ("validator", "observer"):
            for args, inventory in (
                (["--monitoring-only"], f"/etc/seismic/{role}-installation.toml"),
                (
                    ["--inventory", "/etc/seismic/custom.toml", "--monitoring-only"],
                    "/etc/seismic/custom.toml",
                ),
            ):
                with self.subTest(role=role, args=args):
                    result = self.run_entrypoint(role, args)
                    self.assertEqual(
                        result.returncode, 0, result.stdout + result.stderr
                    )
                    self.assertEqual(
                        result.stdout.splitlines(),
                        ["PREFLIGHT", f"AGENT {role} {inventory}"],
                    )

    def test_options_are_checked_before_preflight(self):
        for role in ("validator", "observer"):
            for args in (
                ["--bad"],
                ["--inventory"],
                ["--inventory", "/etc/seismic/other.toml"],
                ["--monitoring-only", "--inventory", "relative"],
                ["--monitoring-only", "--role", "observer"],
            ):
                with self.subTest(role=role, args=args):
                    result = self.run_entrypoint(role, args)
                    self.assertNotEqual(result.returncode, 0)
                    self.assertNotIn("PREFLIGHT", result.stdout)
            result = self.run_entrypoint(role, ["--help"])
            self.assertEqual(result.returncode, 0)
            self.assertIn("--monitoring-only", result.stdout)
            self.assertNotIn("PREFLIGHT", result.stdout)

    def test_no_options_retains_full_installer_path(self):
        for role in ("validator", "observer"):
            result = self.run_entrypoint(role, [])
            self.assertEqual(result.returncode, 99)
            self.assertIn(
                "FORBIDDEN confirm_installation_inventory_overwrite", result.stdout
            )


if __name__ == "__main__":
    unittest.main()

"""Tests for Prometheus Agent installation and monitoring lifecycle."""

from __future__ import annotations

import contextlib
import hashlib
import io
import os
import sys
import tarfile
import tempfile
import unittest
from pathlib import Path
from types import SimpleNamespace
from unittest import mock

ROOT = Path(__file__).resolve().parents[1]
sys.path.insert(0, str(ROOT / "tools"))

from seismic_node import agent, fsutil, monitoring, nodeenv, systemd
from seismic_node.errors import NodeError

EXAMPLE = ROOT / "install/templates/node.env.example"
TOKEN = "a" * 64


def env(**overrides: str) -> nodeenv.NodeEnv:
    values = nodeenv.parse(EXAMPLE.read_text())
    values.update(
        CUSTODIAN_CHAIN_ID="5124",
        MONITORING_ENABLED="true",
        MONITORING_NODE_NAME="node-1.example",
        MONITORING_REMOTE_WRITE_URL="https://metrics.example/api/v1/write",
    )
    values.update(overrides)
    return nodeenv.validate(values)


class SettingsTests(unittest.TestCase):
    def test_settings_from_node_env(self):
        values = agent.settings(env())
        self.assertEqual(values["node"], "node-1.example")
        self.assertEqual(values["role"], "validator")
        self.assertEqual(values["data_dir"], "/var/lib/seismic-prometheus-agent")

    def test_disabled_is_rejected(self):
        with self.assertRaisesRegex(agent.AgentError, "MONITORING_ENABLED is false"):
            agent.settings(env(MONITORING_ENABLED="false"))

    def test_reject_unsafe_endpoint_and_label(self):
        bad_urls = [
            "http://metrics.example/api/v1/write",
            "https://metrics.example:8443/api/v1/write",
            "https://user:pw@metrics.example/api/v1/write",
            "https://metrics.example/api/v1/write?x=1",
            "https://metrics.example/api/v1/write#f",
            "https://metrics.example/other",
            "https://metrics.example/api/v1/write\n",
        ]
        for url in bad_urls:
            with (
                self.subTest(url=url),
                self.assertRaises((agent.AgentError, nodeenv.NodeEnvError)),
            ):
                agent.settings(env(MONITORING_REMOTE_WRITE_URL=url))
        for label in ("Node", "node..x", "-node", "node-"):
            with (
                self.subTest(label=label),
                self.assertRaisesRegex(agent.AgentError, "hostname-style"),
            ):
                agent.settings(env(MONITORING_NODE_NAME=label))

    def test_wal_must_be_separate(self):
        for data_dir in (
            "/etc/seismic/prometheus-agent/wal",
            "/usr/local/lib/seismic/prometheus-agent/wal",
            "/etc",
            "/persistence/reth/wal",
        ):
            with (
                self.subTest(data_dir=data_dir),
                self.assertRaises((agent.AgentError, nodeenv.NodeEnvError)),
            ):
                agent.settings(env(MONITORING_DATA_DIR=data_dir))

    def test_render_both_roles_without_placeholders(self):
        for role_overrides in (
            {},
            {
                "SEISMIC_ROLE": "observer",
                "OBSERVER_PARENT_NODE_PUBLIC_KEY": "ab" * 32,
                "OBSERVER_INDEX": "1",
                "OBSERVER_PUBLIC_ADDRESS": "203.0.113.1:18551",
                "OBSERVER_P2P_PORT": "18551",
                "CUSTODIAN_PARENT_URL": "https://parent.example/custodian",
            },
        ):
            config = agent.render(agent.settings(env(**role_overrides)))
            self.assertNotIn("_PLACEHOLDER", config)
            self.assertIn(f'credentials_file: "{agent.TOKEN}"', config)
            self.assertIn('url: "https://metrics.example/api/v1/write"', config)
            self.assertIn(
                f'role: "{role_overrides.get("SEISMIC_ROLE", "validator")}"', config
            )

    def test_architectures(self):
        self.assertEqual(agent.architecture("x86_64"), "amd64")
        self.assertEqual(agent.architecture("aarch64"), "arm64")
        with self.assertRaises(agent.AgentError):
            agent.architecture("riscv64")


class TokenTests(unittest.TestCase):
    def setUp(self):
        temporary = tempfile.TemporaryDirectory()
        self.addCleanup(temporary.cleanup)
        self.root = Path(temporary.name)
        # Fixture files are owned by the test user, not root.
        patcher = mock.patch.object(
            fsutil, "require_root_managed_file", side_effect=lambda p, d: p.lstat()
        )
        patcher.start()
        self.addCleanup(patcher.stop)

    def write(self, name: str, text: str, mode: int) -> Path:
        path = self.root / name
        path.write_text(text)
        path.chmod(mode)
        return path

    def test_handoff_must_be_root_only_and_well_formed(self):
        self.assertEqual(
            agent.read_token(self.write("t", TOKEN, 0o600), handoff=True),
            TOKEN.encode() + b"\n",
        )
        with self.assertRaisesRegex(agent.AgentError, "root-only"):
            agent.read_token(self.write("g", TOKEN, 0o640), handoff=True)
        agent.read_token(self.write("g2", TOKEN, 0o640), handoff=False)
        with self.assertRaisesRegex(agent.AgentError, "private"):
            agent.read_token(self.write("w", TOKEN, 0o644), handoff=False)
        with self.assertRaisesRegex(agent.AgentError, "64-character"):
            agent.read_token(self.write("b", "not-a-token", 0o600), handoff=True)

    def test_error_messages_never_echo_the_token(self):
        secret = "b" * 64
        with self.assertRaises(agent.AgentError) as caught:
            agent.read_token(self.write("w", secret, 0o644), handoff=True)
        self.assertNotIn(secret, str(caught.exception))


class ReleaseTests(unittest.TestCase):
    def test_checksum_failure_writes_no_executables(self):
        with (
            tempfile.TemporaryDirectory() as tmp,
            mock.patch.object(agent.platform, "machine", return_value="x86_64"),
            mock.patch.object(
                agent.urllib.request, "urlopen", return_value=io.BytesIO(b"wrong")
            ),
            mock.patch.object(agent, "directory") as mkdir,
            mock.patch.object(agent, "atomic_write") as write,
            mock.patch.object(agent, "point_current") as current,
            self.assertRaisesRegex(agent.AgentError, "SHA-256"),
        ):
            agent.install_release(Path(tmp))
        mkdir.assert_not_called()
        write.assert_not_called()
        current.assert_not_called()

    def test_only_expected_regular_executables_are_installed(self):
        data = io.BytesIO()
        with tarfile.open(fileobj=data, mode="w:gz") as tar:
            for name in ("prometheus", "promtool", "../../escape"):
                member = tarfile.TarInfo(
                    f"prometheus-{agent.VERSION}.linux-amd64/{name}"
                )
                member.size = 3
                tar.addfile(member, io.BytesIO(b"bin"))
        archive = data.getvalue()
        with (
            tempfile.TemporaryDirectory() as tmp,
            mock.patch.object(agent.platform, "machine", return_value="x86_64"),
            mock.patch.dict(
                agent.CHECKSUMS, {"amd64": hashlib.sha256(archive).hexdigest()}
            ),
            mock.patch.object(
                agent.urllib.request, "urlopen", return_value=io.BytesIO(archive)
            ),
            mock.patch.object(agent, "directory"),
            mock.patch.object(agent, "atomic_write") as write,
            mock.patch.object(agent, "point_current") as current,
        ):
            agent.install_release(Path(tmp))
        self.assertEqual(
            [c.args[0].name for c in write.call_args_list], ["prometheus", "promtool"]
        )
        current.assert_called_once_with(agent.BIN_DIR)
        self.assertFalse((Path(tmp) / "escape").exists())

    def test_point_current_replaces_symlink_atomically(self):
        with (
            tempfile.TemporaryDirectory() as tmp,
            mock.patch.object(agent, "RELEASES", Path(tmp)),
            mock.patch.object(agent, "CURRENT", Path(tmp) / "current"),
        ):
            (Path(tmp) / "3.4.0").mkdir()
            (Path(tmp) / "3.5.0").mkdir()
            agent.point_current(Path(tmp) / "3.4.0")
            self.assertEqual(os.readlink(Path(tmp) / "current"), "3.4.0")
            agent.point_current(Path(tmp) / "3.5.0")
            self.assertEqual(os.readlink(Path(tmp) / "current"), "3.5.0")
            self.assertEqual(
                sorted(p.name for p in Path(tmp).iterdir()),
                ["3.4.0", "3.5.0", "current"],
            )
            (Path(tmp) / "current").unlink()
            (Path(tmp) / "current").mkdir()
            with self.assertRaisesRegex(agent.AgentError, "not a symbolic link"):
                agent.point_current(Path(tmp) / "3.5.0")


class InstallationTests(unittest.TestCase):
    @contextlib.contextmanager
    def fixture(self):
        with tempfile.TemporaryDirectory() as tmp, contextlib.ExitStack() as stack:
            root = Path(tmp)
            for key, value in {
                "HOME": root / "config",
                "TOKEN": root / "config/token",
                "CONFIG": root / "config/prometheus.yml",
                "RELEASES": root / "releases",
                "BIN_DIR": root / "releases/3.5.0",
                "CURRENT": root / "releases/current",
            }.items():
                stack.enter_context(mock.patch.object(agent, key, value))
            # Fixture-only bypass of host-root ownership; all writes stay in tmp.
            stack.enter_context(mock.patch.object(fsutil, "require_secure_root_parent"))
            stack.enter_context(
                mock.patch.object(
                    fsutil,
                    "require_root_managed_file",
                    side_effect=lambda p, d: p.lstat(),
                )
            )
            stack.enter_context(mock.patch.object(agent.os, "chown"))
            stack.enter_context(mock.patch.object(agent.os, "fchown"))
            stack.enter_context(
                mock.patch.object(
                    agent,
                    "service_account",
                    return_value=SimpleNamespace(
                        pw_uid=os.getuid() or 1000, pw_gid=os.getgid() or 1000
                    ),
                )
            )
            stack.enter_context(mock.patch.object(agent, "install_release"))
            stack.enter_context(
                mock.patch.object(
                    systemd,
                    "status",
                    return_value=systemd.ServiceStatus(
                        "prometheus-agent", "inactive", "dead"
                    ),
                )
            )
            run = stack.enter_context(
                mock.patch.object(
                    agent.subprocess, "run", return_value=SimpleNamespace(returncode=0)
                )
            )
            token = root / "handoff"
            token.write_text(TOKEN)
            token.chmod(0o600)
            yield root, token, run

    @staticmethod
    def reinstall_bypass():
        """Fixture dirs are owned by the tester, not root; keep real contents."""
        return mock.patch.object(
            agent,
            "directory",
            side_effect=lambda p, *a, **kw: p.mkdir(parents=True, exist_ok=True),
        )

    def test_install_and_reinstall_preserve_token_and_wal_and_start_nothing(self):
        with self.fixture() as (root, token, run):
            config = env(MONITORING_DATA_DIR=str(root / "wal"))
            agent.install(config, token)
            (root / "wal/buffered").write_text("samples")
            self.assertEqual(agent.TOKEN.stat().st_mode & 0o777, 0o640)
            self.assertEqual(agent.CONFIG.stat().st_mode & 0o777, 0o640)
            self.assertNotIn(TOKEN, agent.CONFIG.read_text())
            with self.reinstall_bypass():
                agent.install(config, token)
            self.assertEqual(agent.TOKEN.read_text(), TOKEN + "\n")
            self.assertEqual((root / "wal/buffered").read_text(), "samples")
            for call in run.call_args_list:
                self.assertEqual(call.args[0][1:4], ["check", "config", "--agent"])

    def test_refuses_to_adopt_unrelated_wal(self):
        with self.fixture() as (root, token, _):
            (root / "wal").mkdir()
            (root / "wal/stranger").write_text("x")
            with self.assertRaisesRegex(agent.AgentError, "unrelated nonempty WAL"):
                agent.install(env(MONITORING_DATA_DIR=str(root / "wal")), token)

    def test_validation_failure_does_not_replace_existing_configuration(self):
        with self.fixture() as (root, token, run):
            config = env(MONITORING_DATA_DIR=str(root / "wal"))
            agent.install(config, token)
            before = {p: p.read_bytes() for p in (agent.CONFIG, agent.TOKEN)}
            token.write_text("b" * 64)
            run.return_value.returncode = 1
            with (
                self.reinstall_bypass(),
                self.assertRaisesRegex(agent.AgentError, "did not validate"),
            ):
                agent.install(config, token)
            self.assertEqual({p: p.read_bytes() for p in before}, before)

    def test_update_refuses_running_agent(self):
        with (
            self.fixture() as (root, token, _),
            mock.patch.object(
                systemd,
                "status",
                return_value=systemd.ServiceStatus(
                    "prometheus-agent", "active", "running"
                ),
            ),
            self.assertRaisesRegex(agent.AgentError, "Stop the Prometheus Agent"),
        ):
            agent.install(env(MONITORING_DATA_DIR=str(root / "wal")), token)

    def test_token_source_in_node_directories_is_rejected_without_reading_it(self):
        with (
            self.fixture() as (_root, _, _),
            mock.patch.object(agent, "read_token") as read,
            self.assertRaisesRegex(agent.AgentError, "outside node data"),
        ):
            agent.install(env(), Path("/persistence/keys/token"))
        read.assert_not_called()

    def test_check_existing_detects_drift(self):
        with self.fixture() as (root, token, _):
            config = env(MONITORING_DATA_DIR=str(root / "wal"))
            agent.install(config, token)
            agent.BIN_DIR.mkdir(parents=True)
            (agent.BIN_DIR / "prometheus").write_text("bin")
            agent.CURRENT.symlink_to("3.5.0")
            agent.check_existing(config)
            with self.assertRaisesRegex(agent.AgentError, "does not match node.env"):
                agent.check_existing(
                    env(
                        MONITORING_DATA_DIR=str(root / "wal"),
                        MONITORING_NODE_NAME="other",
                    )
                )

    def test_existing_account_must_have_a_private_group(self):
        account = SimpleNamespace(pw_name=agent.USER, pw_uid=999, pw_gid=999)
        shared = SimpleNamespace(gr_name=agent.USER, gr_mem=["someone"])
        with (
            mock.patch.object(agent.pwd, "getpwnam", return_value=account),
            mock.patch.object(agent.pwd, "getpwall", return_value=[account]),
            mock.patch.object(agent.grp, "getgrgid", return_value=shared),
            self.assertRaisesRegex(agent.AgentError, "private service group"),
        ):
            agent.service_account()


class LifecycleTests(unittest.TestCase):
    def test_ensure_running_is_best_effort(self):
        inventory = {"env": env()}
        with mock.patch.object(systemd, "start_service") as start:
            monitoring.ensure_running(inventory, "validator")
        start.assert_called_once_with("prometheus-agent")
        with (
            mock.patch.object(
                systemd, "start_service", side_effect=systemd.ServiceError("x")
            ),
            contextlib.redirect_stderr(io.StringIO()) as err,
        ):
            monitoring.ensure_running(inventory, "validator")
        self.assertIn("WARNING", err.getvalue())

    def test_ensure_running_skips_when_disabled_and_rejects_wrong_role(self):
        with mock.patch.object(systemd, "start_service") as start:
            monitoring.ensure_running(
                {"env": env(MONITORING_ENABLED="false")}, "validator"
            )
        start.assert_not_called()
        with contextlib.redirect_stderr(io.StringIO()) as err:
            monitoring.ensure_running({"env": env()}, "observer")
        self.assertIn("WARNING", err.getvalue())

    def test_commands(self):
        loaded = env()
        with (
            mock.patch.object(fsutil, "require_root"),
            mock.patch.object(nodeenv, "load", return_value=loaded),
            mock.patch.object(agent, "check_existing") as check,
            mock.patch.object(systemd, "reload_units") as reload,
            mock.patch.object(systemd, "start_service") as start,
            mock.patch.object(systemd, "stop_service") as stop,
            mock.patch.object(
                systemd,
                "status",
                return_value=systemd.ServiceStatus(
                    "prometheus-agent", "active", "running"
                ),
            ),
            contextlib.redirect_stdout(io.StringIO()) as out,
        ):
            monitoring.handle(
                SimpleNamespace(monitoring_command="start", node_env=None)
            )
            monitoring.handle(SimpleNamespace(monitoring_command="stop", node_env=None))
            monitoring.handle(
                SimpleNamespace(monitoring_command="status", node_env=None)
            )
            monitoring.handle(
                SimpleNamespace(monitoring_command="check", node_env=None)
            )
        check.assert_called_with(loaded)
        self.assertEqual(check.call_count, 2)
        reload.assert_called_once()
        start.assert_called_once_with("prometheus-agent")
        stop.assert_called_once_with("prometheus-agent")
        self.assertIn(
            "seismic-prometheus-agent.service: active running", out.getvalue()
        )

    def test_install_command_requires_absolute_token_source(self):
        with (
            mock.patch.object(fsutil, "require_root"),
            mock.patch.object(nodeenv, "load", return_value=env()),
            mock.patch.object(agent, "install") as install,
        ):
            with self.assertRaises(NodeError):
                monitoring.handle(
                    SimpleNamespace(
                        monitoring_command="install",
                        node_env=None,
                        token_source=Path("rel"),
                    )
                )
            monitoring.handle(
                SimpleNamespace(
                    monitoring_command="install",
                    node_env=None,
                    token_source=Path("/root/t"),
                )
            )
        install.assert_called_once()

    def test_disabled_status_is_informational_and_start_refuses(self):
        with (
            mock.patch.object(fsutil, "require_root"),
            mock.patch.object(
                nodeenv, "load", return_value=env(MONITORING_ENABLED="false")
            ),
            mock.patch.object(systemd, "start_service") as start,
            contextlib.redirect_stdout(io.StringIO()) as out,
        ):
            monitoring.handle(
                SimpleNamespace(monitoring_command="status", node_env=None)
            )
            with self.assertRaisesRegex(NodeError, "not enabled"):
                monitoring.handle(
                    SimpleNamespace(monitoring_command="start", node_env=None)
                )
        self.assertIn("not enabled", out.getvalue())
        start.assert_not_called()


if __name__ == "__main__":
    unittest.main()

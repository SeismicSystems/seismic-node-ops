"""Tests for Prometheus Agent installation and monitoring lifecycle."""

from __future__ import annotations

import contextlib
import hashlib
import io
import json
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
        DOMAIN="node.example.com",
        MONITORING_MODE="push",
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

    def test_only_push_mode_uses_the_agent(self):
        with self.assertRaisesRegex(agent.AgentError, "MONITORING_MODE is off"):
            agent.settings(env(MONITORING_MODE="off"))
        with self.assertRaisesRegex(agent.AgentError, "MONITORING_MODE is pull"):
            agent.settings(
                env(MONITORING_MODE="pull", TLS_MODE="full", DOMAIN="node.example.com")
            )

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

    def test_render_validator_without_placeholders(self):
        config = agent.render(agent.settings(env()))
        self.assertNotIn("_PLACEHOLDER", config)
        self.assertIn(f'credentials_file: "{agent.TOKEN}"', config)
        self.assertIn('url: "https://metrics.example/api/v1/write"', config)
        self.assertIn('role: "validator"', config)

    def test_observer_pushes_to_the_operator_only(self):
        observer = {
            "SEISMIC_ROLE": "observer",
            "OBSERVER_PARENT_NODE_PUBLIC_KEY": "ab" * 32,
            "OBSERVER_INDEX": "1",
            "OBSERVER_PUBLIC_ADDRESS": "203.0.113.1:18551",
            "OBSERVER_P2P_PORT": "18551",
            "CUSTODIAN_PARENT_URL": "https://parent.example/custodian",
        }
        # node.env validation requires the operator destination for observers.
        with self.assertRaisesRegex(nodeenv.NodeEnvError, "never push to Seismic"):
            env(**observer)
        values = agent.settings(
            env(
                **observer,
                MONITORING_OPERATOR_REMOTE_WRITE_URL="https://prom.op.example/api/v1/write",
            )
        )
        self.assertEqual(values["url"], "")
        self.assertEqual(values["role"], "observer")
        config = agent.render(values)
        self.assertEqual(config.count("- name:"), 1)
        self.assertIn("name: operator", config)
        self.assertNotIn("seismic-monitoring", config)
        self.assertNotIn(str(agent.TOKEN), config)
        self.assertIn('role: "observer"', config)
        self.assertNotIn("_PLACEHOLDER", config)

    def test_operator_remote_write_is_optional_and_rendered_second(self):
        operator = "https://prom.operator.example:9443/api/v1/write"
        # Absent by default: one remote_write entry, no operator token.
        config = agent.render(agent.settings(env()))
        self.assertEqual(config.count("- name:"), 1)
        self.assertNotIn("name: operator", config)
        self.assertNotIn("_PLACEHOLDER", config)

        values = agent.settings(env(MONITORING_OPERATOR_REMOTE_WRITE_URL=operator))
        self.assertEqual(values["operator_url"], operator)
        without = agent.render(values)
        self.assertEqual(without.count("- name:"), 2)
        self.assertLess(
            without.index("name: seismic-monitoring"), without.index("name: operator")
        )
        self.assertIn(f'url: "{operator}"', without)
        self.assertNotIn(str(agent.OPERATOR_TOKEN), without)
        with_token = agent.render(values, operator_token=True)
        self.assertIn(f'credentials_file: "{agent.OPERATOR_TOKEN}"', with_token)
        # Seismic's queue is untouched by the second entry.
        self.assertEqual(with_token.count("retry_on_http_429: true"), 2)

    def test_operator_remote_write_url_rules(self):
        # Any HTTPS port is fine for the operator's own receiver, but the
        # other rules still apply, and it must not duplicate Seismic's.
        agent.settings(
            env(
                MONITORING_OPERATOR_REMOTE_WRITE_URL="https://p.example:9090/api/v1/write"
            )
        )
        for url in (
            "http://p.example/api/v1/write",
            "https://u:p@p.example/api/v1/write",
            "https://p.example/api/v1/write?x=1",
            "https://p.example/other",
            "https://metrics.example/api/v1/write",
        ):
            with (
                self.subTest(url=url),
                self.assertRaises((agent.AgentError, nodeenv.NodeEnvError)),
            ):
                agent.settings(env(MONITORING_OPERATOR_REMOTE_WRITE_URL=url))

    def test_scrape_targets_follow_node_env(self):
        base = agent.scrape_targets(env())
        self.assertEqual(
            [
                (
                    t["labels"]["service"],
                    t["targets"][0],
                    t["labels"]["__metrics_path__"],
                )
                for t in base
            ],
            [
                ("summit", "127.0.0.1:9090", "/"),
                ("reth", "127.0.0.1:9001", "/"),
                ("custodian", "127.0.0.1:7876", "/metrics"),
            ],
        )
        custom = agent.scrape_targets(env(CUSTODIAN_COUNCIL_PORT="17876"))
        self.assertEqual(custom[2]["targets"], ["127.0.0.1:17876"])
        with_checkpointer = agent.scrape_targets(env(CHECKPOINTER_ENABLED="true"))
        self.assertEqual(
            with_checkpointer[3],
            {
                "targets": ["127.0.0.1:42069"],
                "labels": {"service": "checkpointer", "__metrics_path__": "/metrics"},
            },
        )
        # Valid file_sd JSON: a list of target groups, every label a string.
        for group in json.loads(agent.render_targets(env(CHECKPOINTER_ENABLED="true"))):
            self.assertEqual(set(group), {"targets", "labels"})
            self.assertTrue(all(isinstance(v, str) for v in group["labels"].values()))

    def test_template_scrapes_the_targets_file_by_relative_path(self):
        config = agent.render(agent.settings(env()))
        self.assertIn('files: ["targets.json"]', config)
        self.assertIn('replacement: "${1}/node-1.example"', config)
        self.assertNotIn("9090", config, "service ports belong in targets.json")

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
                "OPERATOR_TOKEN": root / "config/operator-token",
                "CONFIG": root / "config/prometheus.yml",
                "TARGETS": root / "config/targets.json",
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

    def test_observer_install_takes_no_seismic_token(self):
        observer = {
            "SEISMIC_ROLE": "observer",
            "OBSERVER_PARENT_NODE_PUBLIC_KEY": "ab" * 32,
            "OBSERVER_INDEX": "1",
            "OBSERVER_PUBLIC_ADDRESS": "203.0.113.1:18551",
            "OBSERVER_P2P_PORT": "18551",
            "CUSTODIAN_PARENT_URL": "https://parent.example/custodian",
            "MONITORING_OPERATOR_REMOTE_WRITE_URL": "https://prom.op.example/api/v1/write",
        }
        with self.fixture() as (root, token, _run):
            config = env(**observer, MONITORING_DATA_DIR=str(root / "wal"))
            # A validator must give Seismic's token; an observer must not.
            with self.assertRaisesRegex(agent.AgentError, "--token-source is required"):
                agent.install(env(MONITORING_DATA_DIR=str(root / "wal")), None)
            with self.assertRaisesRegex(agent.AgentError, "do not push to Seismic"):
                agent.install(config, token)
            op = root / "op-handoff"
            op.write_text("operator-token\n")
            op.chmod(0o600)
            agent.install(config, None, op)
            agent.BIN_DIR.mkdir(parents=True)
            (agent.BIN_DIR / "prometheus").write_text("bin")
            agent.CURRENT.symlink_to("3.5.0")
            rendered = agent.CONFIG.read_text()
            self.assertNotIn("seismic-monitoring", rendered)
            self.assertIn(f'credentials_file: "{agent.OPERATOR_TOKEN}"', rendered)
            self.assertFalse(agent.TOKEN.exists())
            agent.check_existing(config)
            # A Seismic token left over from a validator install is removed.
            agent.TOKEN.write_text(TOKEN + "\n")
            with self.reinstall_bypass():
                agent.install(config, None, op)
            self.assertFalse(agent.TOKEN.exists())
            agent.check_existing(config)

    def test_operator_token_lifecycle(self):
        operator = "https://prom.operator.example/api/v1/write"
        with self.fixture() as (root, token, _run):
            config = env(
                MONITORING_DATA_DIR=str(root / "wal"),
                MONITORING_OPERATOR_REMOTE_WRITE_URL=operator,
            )
            # A token source without the URL in node.env is a mistake.
            with self.assertRaisesRegex(agent.AgentError, "needs MONITORING_OPERATOR"):
                agent.install(env(MONITORING_DATA_DIR=str(root / "wal")), token, token)
            # URL without token: second remote_write, no credentials.
            agent.install(config, token)
            agent.BIN_DIR.mkdir(parents=True)
            (agent.BIN_DIR / "prometheus").write_text("bin")
            agent.CURRENT.symlink_to("3.5.0")
            self.assertIn("name: operator", agent.CONFIG.read_text())
            self.assertNotIn("operator-token", agent.CONFIG.read_text())
            self.assertFalse(agent.OPERATOR_TOKEN.exists())
            agent.check_existing(config)
            # Add a token: operator tokens are opaque, not 64-hex; same
            # handoff rules as Seismic's.
            op = root / "op-handoff"
            op.write_text("glsa_operator-token.value\n")
            op.chmod(0o600)
            with self.reinstall_bypass():
                agent.install(config, token, op)
            self.assertEqual(
                agent.OPERATOR_TOKEN.read_text(), "glsa_operator-token.value\n"
            )
            self.assertEqual(agent.OPERATOR_TOKEN.stat().st_mode & 0o777, 0o640)
            self.assertIn(
                f'credentials_file: "{agent.OPERATOR_TOKEN}"', agent.CONFIG.read_text()
            )
            agent.check_existing(config)
            op.chmod(0o640)
            with (
                self.reinstall_bypass(),
                self.assertRaisesRegex(agent.AgentError, "root-only"),
            ):
                agent.install(config, token, op)
            op.chmod(0o600)
            op.write_text("two words\n")
            with (
                self.reinstall_bypass(),
                self.assertRaisesRegex(agent.AgentError, "single-line"),
            ):
                agent.install(config, token, op)
            # Reinstall without the token removes the stale credential and
            # the reference to it.
            with self.reinstall_bypass():
                agent.install(config, token)
            self.assertFalse(agent.OPERATOR_TOKEN.exists())
            self.assertNotIn("operator-token", agent.CONFIG.read_text())
            agent.check_existing(config)
            # Dropping the URL from node.env is drift until reinstalled.
            with self.assertRaisesRegex(agent.AgentError, "does not match"):
                agent.check_existing(env(MONITORING_DATA_DIR=str(root / "wal")))

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
            self.assertEqual(agent.TARGETS.stat().st_mode & 0o777, 0o640)
            self.assertNotIn(TOKEN, agent.CONFIG.read_text())
            self.assertEqual(agent.TARGETS.read_text(), agent.render_targets(config))
            with self.reinstall_bypass():
                agent.install(config, token)
            self.assertEqual(agent.TOKEN.read_text(), TOKEN + "\n")
            self.assertEqual((root / "wal/buffered").read_text(), "samples")
            for call in run.call_args_list:
                self.assertEqual(call.args[0][1:4], ["check", "config", "--agent"])
                # promtool resolves the relative targets.json against the staged
                # config, so the staged directory must hold both.
                staged = Path(call.args[0][4])
                self.assertEqual(staged.name, "prometheus.yml")
                self.assertEqual(staged.parent.name[:9], ".install-")

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
            before = {
                p: p.read_bytes() for p in (agent.CONFIG, agent.TOKEN, agent.TARGETS)
            }
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
            with self.assertRaisesRegex(agent.AgentError, "prometheus.yml does not"):
                agent.check_existing(
                    env(
                        MONITORING_DATA_DIR=str(root / "wal"),
                        MONITORING_NODE_NAME="other",
                    )
                )
            # Enabling the checkpointer adds a scrape target; the installed
            # targets.json is stale until the agent is reinstalled.
            with self.assertRaisesRegex(agent.AgentError, "targets.json does not"):
                agent.check_existing(
                    env(
                        MONITORING_DATA_DIR=str(root / "wal"),
                        CHECKPOINTER_ENABLED="true",
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
            monitoring.ensure_running({"env": env(MONITORING_MODE="off")}, "validator")
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
            with self.assertRaises(NodeError):
                monitoring.handle(
                    SimpleNamespace(
                        monitoring_command="install",
                        node_env=None,
                        token_source=Path("/root/t"),
                        operator_token_source=Path("rel"),
                    )
                )
            monitoring.handle(
                SimpleNamespace(
                    monitoring_command="install",
                    node_env=None,
                    token_source=Path("/root/t"),
                    operator_token_source=Path("/root/op"),
                )
            )
            # Observers give no Seismic token; the handler passes None through.
            monitoring.handle(
                SimpleNamespace(
                    monitoring_command="install",
                    node_env=None,
                    token_source=None,
                    operator_token_source=Path("/root/op"),
                )
            )
        self.assertEqual(
            [c.args[1:] for c in install.call_args_list],
            [(Path("/root/t"), Path("/root/op")), (None, Path("/root/op"))],
        )

    def test_stop_after_switching_from_push_only_touches_the_agent(self):
        unit = "seismic-prometheus-agent.service"
        for mode in ("off", "pull"):
            for load, state in (
                ("loaded", "active"),
                ("loaded", "inactive"),
                ("not-found", "inactive"),
            ):
                with (
                    self.subTest(mode=mode, load=load, state=state),
                    mock.patch.object(fsutil, "require_root"),
                    mock.patch.object(
                        nodeenv,
                        "load",
                        return_value=env(
                            MONITORING_MODE=mode,
                            MONITORING_NODE_NAME="",
                            MONITORING_REMOTE_WRITE_URL="",
                        ),
                    ),
                    mock.patch.object(agent, "check_existing") as check,
                    mock.patch.object(
                        systemd,
                        "run_systemctl",
                        return_value=SimpleNamespace(
                            returncode=0,
                            stdout=f"LoadState={load}\nActiveState={state}\nSubState=\n",
                            stderr="",
                        ),
                    ) as systemctl,
                    contextlib.redirect_stdout(io.StringIO()) as out,
                ):
                    monitoring.handle(
                        SimpleNamespace(monitoring_command="stop", node_env=None)
                    )
                    expected = [
                        mock.call(
                            "show",
                            unit,
                            "--property=LoadState,ActiveState,SubState,Result",
                        )
                    ]
                    if state == "active":
                        expected.append(mock.call("stop", unit))
                    self.assertEqual(systemctl.call_args_list, expected)
                    check.assert_not_called()
                    self.assertIn("Node services were not changed", out.getvalue())

    def test_status_reports_actual_state_when_push_is_disabled(self):
        unit = "seismic-prometheus-agent.service"
        for mode in ("off", "pull"):
            for state, detail, exists in (
                ("active", "running", True),
                ("inactive", "dead", True),
                ("failed", "failed (exit-code)", True),
                ("missing", unit, False),
            ):
                with (
                    self.subTest(mode=mode, state=state),
                    mock.patch.object(fsutil, "require_root"),
                    mock.patch.object(
                        nodeenv, "load", return_value=env(MONITORING_MODE=mode)
                    ),
                    mock.patch.object(agent, "check_existing") as check,
                    mock.patch.object(systemd, "start_service") as start,
                    mock.patch.object(systemd, "stop_service") as stop,
                    mock.patch.object(
                        systemd,
                        "status",
                        return_value=systemd.ServiceStatus(
                            "prometheus-agent", state, detail, exists=exists
                        ),
                    ) as status,
                    contextlib.redirect_stdout(io.StringIO()) as out,
                ):
                    monitoring.handle(
                        SimpleNamespace(monitoring_command="status", node_env=None)
                    )
                    status.assert_called_once_with("prometheus-agent")
                    self.assertEqual(out.getvalue(), f"{unit}: {state} {detail}\n")
                    check.assert_not_called()
                    start.assert_not_called()
                    stop.assert_not_called()

    def test_start_still_refuses_when_push_is_disabled(self):
        for mode in ("off", "pull"):
            with (
                self.subTest(mode=mode),
                mock.patch.object(fsutil, "require_root"),
                mock.patch.object(
                    nodeenv, "load", return_value=env(MONITORING_MODE=mode)
                ),
                mock.patch.object(agent, "check_existing") as check,
                mock.patch.object(systemd, "run_systemctl") as systemctl,
            ):
                with self.assertRaisesRegex(NodeError, "not enabled"):
                    monitoring.handle(
                        SimpleNamespace(monitoring_command="start", node_env=None)
                    )
                check.assert_not_called()
                systemctl.assert_not_called()


if __name__ == "__main__":
    unittest.main()

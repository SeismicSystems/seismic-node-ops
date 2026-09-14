"""Optional agent tests: local fixtures only, never run the root installer."""

from __future__ import annotations

import argparse
import configparser
import contextlib
import hashlib
import importlib.util
import io
import json
import os
import shutil
import subprocess
import sys
import tarfile
import tempfile
import unittest
from pathlib import Path
from types import SimpleNamespace
from unittest import mock

ROOT = Path(__file__).resolve().parents[1]
sys.path.insert(0, str(ROOT / "tools"))
from seismic_node import checkpoint, monitoring, observer, supervisor, validator


def load(name, path):
    spec = importlib.util.spec_from_file_location(name, path)
    module = importlib.util.module_from_spec(spec)
    spec.loader.exec_module(module)
    return module


agent = load("agent_installer", ROOT / "install/lib/prometheus_agent.py")
cli = load("agent_node_cli", ROOT / "tools/seismic-node.py")
PROMTOOL = os.environ.get("PROMETHEUS_AGENT_PROMTOOL") or shutil.which("promtool")


def settings(**changes):
    return {
        "enabled": True,
        "node": "validator-0.example.com",
        "role": "validator",
        "url": "https://metrics.example.com/api/v1/write",
        "data_dir": "/var/lib/seismic-prometheus-agent",
        "version": "3.5.0",
        **changes,
    }


class SettingsTests(unittest.TestCase):
    def test_architectures_and_checksums(self):
        self.assertEqual(agent.architecture("x86_64"), "amd64")
        self.assertEqual(agent.architecture("aarch64"), "arm64")
        with self.assertRaises(agent.AgentError):
            agent.architecture("i686")
        self.assertEqual(len(agent.CHECKSUMS), 2)
        for checksum in agent.CHECKSUMS.values():
            self.assertRegex(checksum, r"^[a-f0-9]{64}$")

    def test_reject_unsafe_endpoint_and_label(self):
        for url in (
            "http://metrics.example.com/api/v1/write",
            "https://user:secret@metrics.example.com/api/v1/write",
            "https://metrics.example.com/api/v1/write?x=1",
            "https://metrics.example.com/api/v1/write?",
            "https://metrics.example.com/api/v1/write#",
            "https://metrics.example.com/",
            "https://metrics.example.com:9090/api/v1/write",
            "https://metrics.example.com/\napi/v1/write",
        ):
            with self.subTest(url=url), self.assertRaises(agent.AgentError):
                agent.validate(settings(url=url))
        for node in ("../key", "bad\nlabel", 'bad"', "UPPER", "a..b", ""):
            with self.subTest(node=node), self.assertRaises(agent.AgentError):
                agent.validate(settings(node=node))

    def test_wal_must_be_separate_and_normalized(self):
        for path in (
            "/",
            "/etc/agent",
            "/usr/data",
            "/var/lib/../data",
            "/data/",
            "/data/a b",
            "/data/node",
            "/data/node/agent",
        ):
            with self.subTest(path=path), self.assertRaises(agent.AgentError):
                agent.validate(settings(data_dir=path), [Path("/data/node")])
        agent.validate(settings(data_dir="/data/agent"), [Path("/data/node")])

    def test_token_source_in_node_keys_is_rejected_without_reading_it(self):
        arguments = [
            "agent",
            "check",
            "--node",
            "v0",
            "--role",
            "validator",
            "--url",
            "https://metrics.example.com/api/v1/write",
            "--data-dir",
            "/var/lib/seismic-prometheus-agent",
            "--token-source",
            "/keys/summit/node_key.pem",
            "--exclude-path",
            "/keys/summit",
        ]
        with (
            mock.patch.object(sys, "argv", arguments),
            mock.patch.object(agent.os, "geteuid", return_value=0),
            mock.patch.object(agent, "require_managed_or_new"),
            mock.patch.object(agent, "safe_path"),
            mock.patch.object(agent, "read_token") as read,
        ):
            with self.assertRaisesRegex(agent.AgentError, "outside node data"):
                agent.main()
            read.assert_not_called()

    def test_root_file_rejects_untrusted_parents(self):
        with tempfile.TemporaryDirectory() as tmp:
            path = Path(tmp) / "token"
            path.write_text("a" * 64)
            with self.assertRaises(agent.AgentError):
                agent.root_file(path)

    def test_symlink_and_writable_parent_are_refused(self):
        with tempfile.TemporaryDirectory() as tmp:
            directory = Path(tmp)
            (directory / "real").mkdir()
            (directory / "link").symlink_to(
                directory / "real", target_is_directory=True
            )
            with self.assertRaises(agent.AgentError):
                agent.safe_path(directory / "link" / "token")

    def test_existing_account_must_have_a_private_group(self):
        account = SimpleNamespace(pw_uid=998, pw_gid=998, pw_name=agent.USER)
        with (
            mock.patch.object(agent.pwd, "getpwnam", return_value=account),
            mock.patch.object(agent.pwd, "getpwall", return_value=[account]),
            mock.patch.object(
                agent.grp,
                "getgrgid",
                return_value=SimpleNamespace(gr_name=agent.USER, gr_mem=[]),
            ) as group,
        ):
            self.assertEqual(agent.service_account(), account)
            group.return_value.gr_mem = ["unrelated-user"]
            with self.assertRaises(agent.AgentError):
                agent.service_account()
            group.return_value.gr_mem = []
            group.return_value.gr_name = "shared-users"
            with self.assertRaises(agent.AgentError):
                agent.service_account()

    def test_private_token_and_bad_token_redaction(self):
        with tempfile.TemporaryDirectory() as tmp:
            path = Path(tmp) / "token"
            path.write_text("a" * 64 + "\n")
            path.chmod(0o600)
            # Only bypass host-root ownership, retaining real mode and bytes.
            with mock.patch.object(agent, "root_file", side_effect=lambda p: p.stat()):
                self.assertEqual(agent.read_token(path), b"a" * 64 + b"\n")
                path.chmod(0o644)
                with self.assertRaises(agent.AgentError):
                    agent.read_token(path)
                path.chmod(0o600)
                path.write_text("PRIVATE-CREDENTIAL")
                with self.assertRaises(agent.AgentError) as caught:
                    agent.read_token(path)
                self.assertNotIn("PRIVATE-CREDENTIAL", str(caught.exception))

    def test_render_both_roles(self):
        for role in ("validator", "observer"):
            config, service = agent.render(settings(role=role))
            self.assertNotIn("PLACEHOLDER", config + service)
            self.assertIn(f'role: "{role}"', config)
            for stream in ("summit", "reth", "prometheus-agent"):
                self.assertIn(f'job_name: "{stream}/validator-0.example.com"', config)
            for port in (9090, 9001, 9091):
                self.assertIn(f"127.0.0.1:{port}", config)
            self.assertIn("credentials_file:", config)
            self.assertIn("retry_on_http_429: true", config)
            self.assertNotIn("insecure_skip_verify", config)
            parser = configparser.ConfigParser(interpolation=None)
            parser.read_string(service)
            program = parser["program:prometheus-agent"]
            self.assertFalse(program.getboolean("autostart"))
            self.assertTrue(program.getboolean("autorestart"))
            self.assertEqual(program["user"], "seismic-prometheus")
            self.assertIn("--web.listen-address=127.0.0.1:9091", program["command"])
            self.assertIn("--storage.agent.retention.max-time=6h", program["command"])

    def test_supervisor_parses_generated_service(self):
        try:
            from supervisor.options import ServerOptions
        except ModuleNotFoundError as error:
            if error.name != "supervisor":
                raise
            self.skipTest("supervisor package unavailable")
        with tempfile.TemporaryDirectory() as tmp:
            root = Path(tmp)
            service = agent.render(settings())[1]
            service = service.replace(str(agent.LOG_DIR), str(root))
            service = service.replace("user=seismic-prometheus", f"user={os.getuid()}")
            (root / "agent.conf").write_text(service)
            wrapper = root / "supervisord.conf"
            wrapper.write_text(
                f"[supervisord]\nlogfile={root}/supervisor.log\npidfile={root}/pid\n"
                f"[include]\nfiles={root}/agent.conf\n"
            )
            options = ServerOptions()
            options.realize(["-c", str(wrapper)])
            self.assertEqual(len(options.process_group_configs), 1)
            group = options.process_group_configs[0]
            self.assertEqual(group.name, "prometheus-agent")
            self.assertFalse(group.process_configs[0].autostart)
            self.assertIn("--agent", group.process_configs[0].command)

    @unittest.skipUnless(PROMTOOL, "promtool unavailable")
    def test_config_validated_in_agent_mode(self):
        with tempfile.TemporaryDirectory() as tmp:
            token = Path(tmp) / "token"
            token.write_text("a" * 64)
            path = Path(tmp) / "prometheus.yml"
            for role in ("validator", "observer"):
                path.write_text(agent.render(settings(role=role), token)[0])
                result = subprocess.run(
                    [PROMTOOL, "check", "config", "--agent", str(path)],
                    check=False,
                    capture_output=True,
                    text=True,
                )
                self.assertEqual(result.returncode, 0, result.stdout + result.stderr)

    def test_checksum_failure_writes_no_executables(self):
        with (
            tempfile.TemporaryDirectory() as tmp,
            mock.patch.object(agent.platform, "machine", return_value="x86_64"),
            mock.patch.object(
                agent.urllib.request,
                "urlopen",
                return_value=io.BytesIO(b"wrong-release"),
            ),
            mock.patch.object(agent, "directory") as mkdir,
            mock.patch.object(agent, "atomic_write") as write,
        ):
            with self.assertRaisesRegex(agent.AgentError, "SHA-256"):
                agent.install_release(Path(tmp))
            mkdir.assert_not_called()
            write.assert_not_called()

    def test_only_expected_regular_executables_are_installed(self):
        data = io.BytesIO()
        with tarfile.open(fileobj=data, mode="w:gz") as tar:
            for name in ("prometheus", "promtool", "../../escape"):
                member = tarfile.TarInfo(f"prometheus-3.5.0.linux-amd64/{name}")
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
        ):
            agent.install_release(Path(tmp))
            self.assertEqual(
                [c.args[0].name for c in write.call_args_list],
                ["prometheus", "promtool"],
            )
            self.assertFalse((Path(tmp) / "escape").exists())


class InstallationTests(unittest.TestCase):
    def test_first_disabled_install_never_controls_services(self):
        with (
            mock.patch.object(agent, "load_settings", return_value=None),
            mock.patch.object(agent, "SUPERVISOR_CONFIG") as conf,
            mock.patch.object(agent.subprocess, "run") as run,
        ):
            conf.exists.return_value = False
            agent.disable()
            run.assert_not_called()

    def test_disabled_mode_ignores_but_never_adopts_unmanaged_agent(self):
        with tempfile.TemporaryDirectory() as tmp:
            root = Path(tmp)
            conf = root / "agent.conf"
            conf.write_text("unrelated program")
            with (
                mock.patch.object(agent, "SETTINGS", root / "absent.json"),
                mock.patch.object(agent, "SUPERVISOR_CONFIG", conf),
                mock.patch.object(agent.subprocess, "run") as run,
            ):
                self.assertIsNone(agent.load_settings())
                with self.assertRaisesRegex(agent.AgentError, "unmanaged"):
                    agent.install(settings(), root / "token")
                run.assert_not_called()
            self.assertEqual(conf.read_text(), "unrelated program")

    def test_update_refuses_running_agent(self):
        with (
            mock.patch.object(agent, "read_token", return_value=b"a" * 64),
            mock.patch.object(agent, "load_settings", return_value=settings()),
            mock.patch.object(agent, "SUPERVISOR_CONFIG") as conf,
            mock.patch.object(agent, "supervisor_state", return_value="RUNNING"),
            mock.patch.object(agent, "install_release") as release,
        ):
            conf.exists.return_value = True
            with self.assertRaisesRegex(agent.AgentError, "Stop prometheus-agent"):
                agent.install(settings(), Path("/root/token"))
            release.assert_not_called()

    def test_disable_stops_only_agent_and_preserves_files(self):
        with tempfile.TemporaryDirectory() as tmp:
            root = Path(tmp)
            conf = root / "supervisor.conf"
            conf.write_text("managed")
            token = root / "token"
            token.write_text("secret")
            wal = root / "wal"
            wal.write_text("buffered")
            with (
                mock.patch.object(agent, "load_settings", return_value=settings()),
                mock.patch.object(
                    agent,
                    "supervisor_state",
                    side_effect=["EXITED", "RUNNING", "STOPPED", "STOPPED"],
                ),
                mock.patch.object(agent, "SUPERVISOR_CONFIG", conf),
                mock.patch.object(agent, "root_file"),
                mock.patch.object(agent, "atomic_write") as write,
                mock.patch.object(
                    agent.subprocess, "run", return_value=SimpleNamespace(returncode=0)
                ) as run,
            ):
                agent.disable()
                self.assertEqual(
                    run.call_args.args[0],
                    ["/usr/bin/supervisorctl", "stop", "prometheus-agent"],
                )
                self.assertFalse(json.loads(write.call_args.args[1])["enabled"])
            self.assertFalse(conf.exists())
            self.assertEqual(token.read_text(), "secret")
            self.assertEqual(wal.read_text(), "buffered")

    def test_failed_disable_preserves_enabled_config(self):
        with (
            mock.patch.object(agent, "load_settings", return_value=settings()),
            mock.patch.object(agent, "supervisor_state", return_value="RUNNING"),
            mock.patch.object(
                agent.subprocess, "run", return_value=SimpleNamespace(returncode=1)
            ),
            mock.patch.object(agent, "atomic_write") as write,
        ):
            with self.assertRaises(agent.AgentError):
                agent.disable()
            write.assert_not_called()

    @contextlib.contextmanager
    def fixture(self):
        with tempfile.TemporaryDirectory() as tmp, contextlib.ExitStack() as stack:
            root = Path(tmp)
            paths = {
                "HOME": root / "config",
                "SETTINGS": root / "config/installation.json",
                "TOKEN": root / "config/token",
                "CONFIG": root / "config/prometheus.yml",
                "SUPERVISOR_CONFIG": root / "supervisor.conf",
                "LOG_DIR": root / "logs",
                "BIN_DIR": root / "binaries/3.5.0",
            }
            for key, value in paths.items():
                stack.enter_context(mock.patch.object(agent, key, value))
            # Fixture-only bypass of host-root ownership; all writes remain in tmp.
            stack.enter_context(mock.patch.object(agent, "safe_path"))
            stack.enter_context(
                mock.patch.object(agent, "root_file", side_effect=lambda p: p.stat())
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
                mock.patch.object(agent, "supervisor_state", return_value="STOPPED")
            )
            run = stack.enter_context(
                mock.patch.object(
                    agent.subprocess, "run", return_value=SimpleNamespace(returncode=0)
                )
            )
            token = root / "handoff"
            token.write_text("a" * 64)
            token.chmod(0o600)
            yield root, token, run

    def test_install_and_reinstall_preserve_token_wal_and_do_not_activate(self):
        with self.fixture() as (root, token, run):
            config = settings(data_dir=str(root / "wal"))
            agent.install(config, token)
            (root / "wal/buffered").write_text("samples")
            self.assertEqual(agent.TOKEN.stat().st_mode & 0o777, 0o640)
            self.assertEqual(agent.SETTINGS.stat().st_mode & 0o777, 0o600)
            self.assertEqual(agent.CONFIG.stat().st_mode & 0o777, 0o640)
            self.assertNotIn("a" * 64, agent.CONFIG.read_text())
            self.assertNotIn("a" * 64, agent.SETTINGS.read_text())
            # Existing fixture directories are owned by the unprivileged tester;
            # preserve the real directory contents while bypassing chown policy.
            with mock.patch.object(
                agent,
                "directory",
                side_effect=lambda p, *a, **kw: p.mkdir(parents=True, exist_ok=True),
            ):
                agent.install(config, agent.TOKEN)
            self.assertEqual(agent.TOKEN.read_text(), "a" * 64 + "\n")
            self.assertEqual((root / "wal/buffered").read_text(), "samples")
            for call in run.call_args_list:
                self.assertEqual(call.args[0][1:4], ["check", "config", "--agent"])

    def test_validation_failure_does_not_replace_existing_configuration(self):
        with self.fixture() as (root, token, run):
            config = settings(data_dir=str(root / "wal"))
            agent.install(config, token)
            before = {
                p: p.read_bytes()
                for p in (
                    agent.CONFIG,
                    agent.TOKEN,
                    agent.SETTINGS,
                    agent.SUPERVISOR_CONFIG,
                )
            }
            token.write_text("b" * 64)
            run.return_value.returncode = 1
            with (
                mock.patch.object(
                    agent,
                    "directory",
                    side_effect=lambda p, *a, **kw: p.mkdir(
                        parents=True, exist_ok=True
                    ),
                ),
                self.assertRaises(agent.AgentError),
            ):
                agent.install(config, token)
            self.assertEqual({p: p.read_bytes() for p in before}, before)

    def test_installer_wiring_and_keep_is_noop(self):
        for role in ("validator", "observer"):
            source = (ROOT / f"install/install-{role}.sh").read_text()
            self.assertIn('source "$SCRIPT_DIR/lib/prometheus-agent.sh"', source)
            self.assertIn("monitoring=$(write_prometheus_agent_inventory)", source)
            self.assertLess(
                source.index("    validate_prometheus_agent_plan"),
                source.index("    install_system_packages"),
            )
        result = subprocess.run(
            [
                "bash",
                "-c",
                f'set -eu; SCRIPT_DIR="{ROOT}/install"; source "$SCRIPT_DIR/lib/prometheus-agent.sh"; python3() {{ exit 99; }}; install_prometheus_agent',
            ],
            check=False,
            capture_output=True,
        )
        self.assertEqual(result.returncode, 0)


class LifecycleTests(unittest.TestCase):
    def test_all_node_start_modes_ensure_agent_after_success(self):
        for role, module, function in (
            ("validator", validator, validator.start_validator),
            ("observer", observer, observer.start_observer),
        ):
            for mode in ("normal", "checkpoint"):
                inventory = {"monitoring": settings(role=role)}
                args = argparse.Namespace(
                    inventory=Path("/custom/inventory.toml"),
                    mode=mode,
                    startup_timeout=3,
                )
                events = []
                with (
                    self.subTest(role=role, mode=mode),
                    mock.patch.object(
                        checkpoint, "load_inventory", return_value=inventory
                    ),
                    mock.patch.object(
                        checkpoint, "validate_checkpoint_start_configuration"
                    ),
                    mock.patch.object(supervisor, "prepare_supervisor"),
                    mock.patch.object(
                        supervisor,
                        "start_node",
                        side_effect=lambda *a, events=events, **kw: events.append(
                            "node"
                        ),
                    ),
                    mock.patch.object(
                        supervisor,
                        "start_program",
                        side_effect=lambda *a, events=events, **kw: events.append(a[0]),
                    ),
                ):
                    function(args)
                    self.assertEqual(events, ["node", "prometheus-agent"])

    def test_disabled_and_old_inventories_are_noops(self):
        for inventory in ({}, {"monitoring": {"enabled": False}}):
            with mock.patch.object(supervisor, "start_program") as start:
                monitoring.ensure_running(inventory, "validator", 3)
                start.assert_not_called()

    def test_agent_failure_warns_without_node_rollback(self):
        inventory = {"monitoring": settings()}
        for error in (
            supervisor.SupervisorError("private output"),
            OSError("private output"),
        ):
            with (
                mock.patch.object(supervisor, "start_program", side_effect=error),
                mock.patch.object(supervisor, "stop_program") as stop,
                contextlib.redirect_stderr(io.StringIO()) as err,
            ):
                monitoring.ensure_running(inventory, "validator", 3)
                self.assertIn("WARNING", err.getvalue())
                self.assertNotIn("private output", err.getvalue())
                stop.assert_not_called()
        with contextlib.redirect_stderr(io.StringIO()) as err:
            monitoring.ensure_running({"monitoring": "broken"}, "validator", 3)
            self.assertIn("WARNING", err.getvalue())

    def test_node_start_failure_does_not_start_or_stop_agent(self):
        for role, start in (
            ("validator", validator.start_validator),
            ("observer", observer.start_observer),
        ):
            args = argparse.Namespace(inventory=None, mode="normal", startup_timeout=3)
            with (
                mock.patch.object(
                    checkpoint,
                    "load_inventory",
                    return_value={"monitoring": settings(role=role)},
                ),
                mock.patch.object(supervisor, "prepare_supervisor"),
                mock.patch.object(
                    supervisor,
                    "start_node",
                    side_effect=supervisor.SupervisorError("node failed"),
                ),
                mock.patch.object(monitoring, "ensure_running") as ensure,
            ):
                with self.assertRaises(supervisor.SupervisorError):
                    start(args)
                ensure.assert_not_called()

    def test_refused_onboarding_does_not_start_agent(self):
        args = argparse.Namespace(mode="normal")
        with (
            mock.patch.object(
                validator,
                "wait_for_start_authorization",
                return_value=validator.StartDecision(False),
            ),
            mock.patch.object(validator, "start_validator") as start,
            mock.patch.object(monitoring, "ensure_running") as ensure,
        ):
            validator.start_onboarded_validator(
                args, "a" * 64, allow_pre_joining_start=False
            )
            start.assert_not_called()
            ensure.assert_not_called()

    def test_agent_stop_does_not_treat_exited_as_quiescent(self):
        states = [
            supervisor.ProgramStatus("prometheus-agent", state, "")
            for state in ("EXITED", "RUNNING", "STOPPED")
        ]
        with (
            mock.patch.object(supervisor, "status", side_effect=states),
            mock.patch.object(
                supervisor,
                "run_supervisorctl",
                side_effect=[
                    SimpleNamespace(returncode=7),
                    SimpleNamespace(returncode=0),
                ],
            ) as control,
            mock.patch.object(supervisor.time, "sleep"),
        ):
            supervisor.stop_autorestarting_program("prometheus-agent")
            self.assertEqual(
                control.call_args_list, [mock.call("stop", "prometheus-agent")] * 2
            )

    def test_node_stop_leaves_agent_alone(self):
        with mock.patch.object(supervisor, "stop_program", return_value=True) as stop:
            supervisor.stop_node(("summit", "summit-checkpoint"))
            self.assertNotIn(mock.call("prometheus-agent"), stop.call_args_list)

    def test_explicit_commands_and_parser(self):
        for action in ("start", "status", "stop"):
            with mock.patch.object(
                sys,
                "argv",
                [
                    "seismic-node.py",
                    "monitoring",
                    action,
                    "--role",
                    "observer",
                    "--inventory",
                    "/custom.toml",
                ],
            ):
                args = cli.parse_args()
            with (
                mock.patch.object(
                    checkpoint,
                    "read_toml",
                    return_value={"monitoring": settings(role="observer")},
                ) as read,
                mock.patch.object(monitoring, "prepare_agent") as prepare,
                mock.patch.object(supervisor, "start_program") as start,
                mock.patch.object(supervisor, "stop_autorestarting_program") as stop,
                mock.patch.object(
                    supervisor, "status", return_value=SimpleNamespace(state="RUNNING")
                ),
            ):
                monitoring.handle(args)
                self.assertEqual(read.call_args.args[0], Path("/custom.toml"))
                self.assertEqual(start.called, action == "start")
                self.assertEqual(stop.called, action == "stop")
                self.assertEqual(prepare.called, action == "start")

    def test_explicit_start_updates_only_agent_group(self):
        result = SimpleNamespace(returncode=0, stdout="", stderr="")
        with (
            mock.patch.object(supervisor, "run_systemctl", return_value=result),
            mock.patch.object(
                supervisor, "run_supervisorctl", return_value=result
            ) as control,
        ):
            monitoring.prepare_agent()
            self.assertEqual(
                control.call_args_list,
                [mock.call("reread"), mock.call("update", "prometheus-agent")],
            )

    def test_inventory_accepts_optional_monitoring_without_weakening_base(self):
        data = {
            "schema_version": 1,
            "reth_data_dir": "/data/reth",
            "reth_p2p_key_path": "/keys/reth-key",
            "summit_data_dir": "/data/summit",
            "summit_keys_dir": "/keys/summit",
            "monitoring": settings(),
        }
        with (
            mock.patch.object(checkpoint, "read_toml", return_value=data),
            mock.patch.object(checkpoint, "validate_installed_identity") as identity,
        ):
            loaded = checkpoint.load_inventory("validator", Path("/inventory.toml"))
            self.assertTrue(monitoring.configured(loaded, "validator"))
            identity.assert_called_once()
        data["unexpected"] = True
        with (
            mock.patch.object(checkpoint, "read_toml", return_value=data),
            self.assertRaises(checkpoint.CheckpointError),
        ):
            checkpoint.load_inventory("validator", Path("/inventory.toml"))

    def test_already_running_is_not_restarted(self):
        with (
            mock.patch.object(
                supervisor,
                "status",
                return_value=supervisor.ProgramStatus(
                    "prometheus-agent", "RUNNING", ""
                ),
            ),
            mock.patch.object(supervisor, "run_supervisorctl") as control,
        ):
            monitoring.ensure_running({"monitoring": settings()}, "validator", 3)
            control.assert_not_called()


if __name__ == "__main__":
    unittest.main()

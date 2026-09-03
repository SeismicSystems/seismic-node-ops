"""Tests for checkpoint acquisition and node-onboarding decisions."""

from __future__ import annotations

import hashlib
import importlib.util
import io
import json
import subprocess
import sys
import tempfile
import threading
import unittest
from collections.abc import Callable
from contextlib import redirect_stderr
from http.server import BaseHTTPRequestHandler, ThreadingHTTPServer
from pathlib import Path
from types import SimpleNamespace
from unittest import mock

TOOLS = Path(__file__).resolve().parents[1] / "tools"
sys.path.insert(0, str(TOOLS))

from seismic_node import checkpoint, download, observer, rpc, supervisor, validator

CLI_SPEC = importlib.util.spec_from_file_location(
    "seismic_node_cli", TOOLS / "seismic-node.py"
)
assert CLI_SPEC is not None and CLI_SPEC.loader is not None
node_cli = importlib.util.module_from_spec(CLI_SPEC)
CLI_SPEC.loader.exec_module(node_cli)


class FixtureHandler(BaseHTTPRequestHandler):
    archive = b"checkpoint-archive"
    manifest = b""

    def do_GET(self) -> None:
        if self.path == "/snapshot":
            self.send_response(200)
            self.send_header("Content-Length", str(len(self.archive)))
            self.end_headers()
            self.wfile.write(self.archive)
            return
        if self.path == "/checkpointer/snapshots/13/manifest":
            self.send_response(200)
            self.send_header("Content-Length", str(len(self.manifest)))
            self.end_headers()
            self.wfile.write(self.manifest)
            return
        if self.path == "/checkpointer/snapshots/13":
            self.send_response(200)
            self.send_header("Content-Length", str(len(self.archive)))
            self.end_headers()
            self.wfile.write(self.archive)
            return
        if self.path == "/redirect":
            self.send_response(302)
            self.send_header("Location", "/snapshot")
            self.end_headers()
            return
        self.send_response(404)
        self.end_headers()

    def do_POST(self) -> None:
        request = json.loads(self.rfile.read(int(self.headers["Content-Length"])))
        if request["method"] == "getLatestEpoch":
            result: object = 14
        elif request["method"] == "getFinalizedHeaderDigest":
            result = {"epoch": 13, "digest": [0xAA] * 32}
        else:
            result = None
        body = json.dumps(
            {"jsonrpc": "2.0", "id": request["id"], "result": result}
        ).encode()
        self.send_response(200)
        self.send_header("Content-Length", str(len(body)))
        self.end_headers()
        self.wfile.write(body)

    def log_message(self, format: str, *args: object) -> None:
        return


class NetworkTests(unittest.TestCase):
    @classmethod
    def setUpClass(cls) -> None:
        cls.server = ThreadingHTTPServer(("127.0.0.1", 0), FixtureHandler)
        cls.thread = threading.Thread(target=cls.server.serve_forever, daemon=True)
        cls.thread.start()
        cls.base_url = f"http://127.0.0.1:{cls.server.server_port}"

    @classmethod
    def tearDownClass(cls) -> None:
        cls.server.shutdown()
        cls.server.server_close()
        cls.thread.join()

    def test_remote_http_is_rejected_but_loopback_is_allowed(self) -> None:
        with self.assertRaisesRegex(rpc.NetworkError, "must use HTTPS"):
            rpc.validate_url("http://snapshot.example/checkpointer", "Snapshot URL")
        rpc.validate_url("http://127.0.0.1:42069", "Snapshot URL")

    def test_invalid_url_port_is_an_operator_error(self) -> None:
        with self.assertRaisesRegex(rpc.NetworkError, "Invalid Summit RPC URL"):
            rpc.validate_url("https://node.example:invalid/rpc", "Summit RPC URL")

    def test_streaming_download_hash_and_redirect_policy(self) -> None:
        with tempfile.TemporaryDirectory() as temporary_directory:
            destination = Path(temporary_directory) / "archive.tar.gz"
            expected_hash = "0x" + hashlib.sha256(FixtureHandler.archive).hexdigest()
            rpc.download_verified_archive(
                f"{self.base_url}/snapshot",
                destination,
                token=None,
                timeout=5.0,
                expected_size=len(FixtureHandler.archive),
                expected_sha256=expected_hash,
            )
            self.assertEqual(destination.read_bytes(), FixtureHandler.archive)
            with self.assertRaisesRegex(rpc.NetworkError, "Redirects are not allowed"):
                rpc.request_bytes(
                    f"{self.base_url}/redirect",
                    token=None,
                    timeout=5.0,
                    maximum_size=1024,
                    description="redirect test",
                )

    def test_failed_exclusive_create_preserves_existing_archive(self) -> None:
        with tempfile.TemporaryDirectory() as temporary_directory:
            destination = Path(temporary_directory) / "archive.tar.gz"
            destination.write_bytes(b"preserve-me")
            with self.assertRaises(FileExistsError):
                rpc.download_verified_archive(
                    f"{self.base_url}/snapshot",
                    destination,
                    token=None,
                    timeout=5.0,
                    expected_size=len(FixtureHandler.archive),
                    expected_sha256=(
                        "0x" + hashlib.sha256(FixtureHandler.archive).hexdigest()
                    ),
                )
            self.assertEqual(destination.read_bytes(), b"preserve-me")

    def test_json_rpc_request(self) -> None:
        self.assertEqual(
            rpc.json_rpc(f"{self.base_url}/rpc", "getLatestEpoch", []),
            14,
        )

    def test_remote_checkpoint_and_rpc_anchor_are_resolved(self) -> None:
        archive_hash = "0x" + hashlib.sha256(FixtureHandler.archive).hexdigest()
        FixtureHandler.manifest = json.dumps(
            {
                "version": 1,
                "epoch": 13,
                "summit_checkpoint_digest": "0x" + "11" * 32,
                "execution": {
                    "block_number": 100,
                    "block_hash": "0x" + "22" * 32,
                    "state_root": "0x" + "33" * 32,
                },
                "archive": {
                    "sha256": archive_hash,
                    "size_bytes": len(FixtureHandler.archive),
                },
                "created_at": "2026-01-01T00:00:00Z",
            }
        ).encode()
        args = SimpleNamespace(
            archive=None,
            manifest=None,
            snapshot_api_url=f"{self.base_url}/checkpointer",
            snapshot_bearer_token_file=None,
            checkpoint_epoch=None,
            checkpoint_policy="ask",
            weak_subjectivity_path=None,
            weak_subjectivity_url=None,
            weak_subjectivity_rpc_url=(
                f"http://localhost:{self.server.server_port}/summit"
            ),
            weak_subjectivity_bearer_token_file=None,
            inventory=None,
            snapshot_wait_timeout=1.0,
            snapshot_poll_interval=0.01,
            http_timeout=5.0,
        )
        with tempfile.TemporaryDirectory() as temporary_directory:
            work_dir = Path(temporary_directory) / "work"
            work_dir.mkdir()
            with (
                mock.patch.object(
                    download, "create_download_work_dir", return_value=work_dir
                ),
                mock.patch.object(
                    checkpoint,
                    "require_root_managed_file",
                    side_effect=checkpoint.require_regular_file,
                ),
                download.resolve_checkpoint_inputs(
                    args,
                    "validator",
                    network_rpc_url=args.weak_subjectivity_rpc_url,
                ) as resolved,
            ):
                self.assertEqual(resolved.epoch, 13)
                self.assertEqual(resolved.archive.read_bytes(), FixtureHandler.archive)
                self.assertIn("epoch = 13", resolved.weak_subjectivity.read_text())

    def test_checkpoint_policy_never_silently_changes_explicit_epoch(self) -> None:
        self.assertEqual(download.choose_newer_checkpoint(12, 13, "exact"), 12)
        self.assertEqual(
            download.choose_newer_checkpoint(12, 13, "latest-available"), 13
        )
        with self.assertRaisesRegex(checkpoint.CheckpointError, "Checkpoint 13"):
            download.choose_newer_checkpoint(12, 13, "fail-if-newer")

    def test_weak_subjectivity_rpc_is_converted_to_toml(self) -> None:
        with tempfile.TemporaryDirectory() as temporary_directory:
            destination = Path(temporary_directory) / "anchor.toml"
            result = {"epoch": 13, "digest": [0xAB] * 32}
            with mock.patch.object(rpc, "json_rpc", return_value=result):
                download.weak_subjectivity_from_rpc(
                    "https://trust.example/summit",
                    13,
                    destination,
                    None,
                    10.0,
                )
            self.assertEqual(
                destination.read_text(),
                'epoch = 13\nheader_digest = "0x' + "ab" * 32 + '"\n',
            )

    def test_same_origin_requires_noninteractive_override(self) -> None:
        args = SimpleNamespace(allow_same_origin_weak_subjectivity=False)
        warnings = io.StringIO()
        with (
            mock.patch.object(sys.stdin, "isatty", return_value=False),
            redirect_stderr(warnings),
            self.assertRaisesRegex(
                checkpoint.CheckpointError,
                "--allow-same-origin-weak-subjectivity",
            ),
        ):
            download.confirm_same_origin_weak_subjectivity(
                args,
                "https://node.example/checkpointer",
                "https://node.example/summit",
            )
        self.assertIn("not independently sourced", warnings.getvalue())

    def test_same_origin_can_be_confirmed_interactively(self) -> None:
        args = SimpleNamespace(allow_same_origin_weak_subjectivity=False)
        with (
            mock.patch.object(sys.stdin, "isatty", return_value=True),
            mock.patch("builtins.input", return_value="yes") as prompt,
            redirect_stderr(io.StringIO()),
        ):
            download.confirm_same_origin_weak_subjectivity(
                args,
                "https://node.example/checkpointer",
                "https://node.example/summit",
            )
        prompt.assert_called_once_with(
            "Continue with same-origin weak subjectivity? [y/N] "
        )

    def test_same_origin_can_be_explicitly_allowed(self) -> None:
        args = SimpleNamespace(allow_same_origin_weak_subjectivity=True)
        warnings = io.StringIO()
        with redirect_stderr(warnings):
            download.confirm_same_origin_weak_subjectivity(
                args,
                "https://node.example/checkpointer",
                "https://node.example/summit",
            )
        self.assertIn(
            "--allow-same-origin-weak-subjectivity was provided",
            warnings.getvalue(),
        )


class CliTests(unittest.TestCase):
    def install_options(self) -> SimpleNamespace:
        return SimpleNamespace(
            archive=None,
            manifest=None,
            snapshot_api_url=None,
            snapshot_bearer_token_file=None,
            checkpoint_epoch=None,
            checkpoint_policy="ask",
            weak_subjectivity_path=None,
            weak_subjectivity_url=None,
            weak_subjectivity_rpc_url=None,
            weak_subjectivity_bearer_token_file=None,
            allow_same_origin_weak_subjectivity=False,
            checkpoint_path=None,
            installed_weak_subjectivity_path=(
                checkpoint.DEFAULT_INSTALLED_WEAK_SUBJECTIVITY_PATH
            ),
            backup_root=None,
            confirm_hostname=None,
            confirm_epoch=None,
        )

    def test_install_modifiers_without_source_get_direct_error(self) -> None:
        for attribute, value in (
            ("checkpoint_policy", "exact"),
            ("allow_same_origin_weak_subjectivity", True),
            (
                "installed_weak_subjectivity_path",
                Path("/etc/seismic/custom-weak-subjectivity.toml"),
            ),
        ):
            with self.subTest(attribute=attribute):
                args = self.install_options()
                setattr(args, attribute, value)
                source_requested = node_cli.checkpoint_source_requested(args)
                self.assertFalse(source_requested)
                with self.assertRaisesRegex(
                    checkpoint.CheckpointError,
                    "require --snapshot-api-url or local --archive and --manifest",
                ):
                    node_cli.require_checkpoint_source_for_install_options(
                        args,
                        source_requested,
                    )


class ValidatorTests(unittest.TestCase):
    def deposit_response(self) -> dict[str, object]:
        address = bytes.fromhex(validator.WITHDRAWAL_ADDRESS.removeprefix("0x"))
        return {
            "jsonrpc": "2.0",
            "id": 1,
            "result": {
                "node_pubkey": [1] * 32,
                "consensus_pubkey": [2] * 48,
                "withdrawal_credentials": [1] + [0] * 11 + list(address),
                "node_signature": [3] * 64,
                "consensus_signature": [4] * 96,
                "deposit_data_root": [5] * 32,
            },
        }

    def test_deposit_response_uses_required_withdrawal_address(self) -> None:
        public_key = validator.validate_deposit_response(self.deposit_response())
        self.assertEqual(public_key, "01" * 32)
        invalid = self.deposit_response()
        invalid["result"]["withdrawal_credentials"][-1] ^= 1  # type: ignore[index]
        with self.assertRaisesRegex(checkpoint.CheckpointError, "required address"):
            validator.validate_deposit_response(invalid)

    def test_deposit_generation_uses_fixed_request_and_stops_rpc(self) -> None:
        response = self.deposit_response()
        args = SimpleNamespace(
            inventory=None,
            output=Path("/tmp/deposit-signature.json"),
            overwrite=False,
            startup_timeout=10.0,
            http_timeout=5.0,
        )
        calls: list[tuple[str, list[object]]] = []
        stopped: list[str] = []

        def fake_rpc(
            url: str,
            method: str,
            params: list[object],
            *,
            timeout: float,
        ) -> dict[str, object]:
            self.assertEqual(url, validator.DEPOSIT_RPC_URL)
            calls.append((method, params))
            return response

        def fake_deposit_start(
            name: str,
            timeout: float,
            *,
            on_start_requested: Callable[[], None],
        ) -> bool:
            on_start_requested()
            return True

        with (
            mock.patch.object(checkpoint, "require_root"),
            mock.patch.object(checkpoint, "load_inventory"),
            mock.patch.object(checkpoint, "require_absolute"),
            mock.patch.object(validator, "confirm_output_overwrite"),
            mock.patch.object(supervisor, "require_stopped"),
            mock.patch.object(
                supervisor,
                "start_program",
                side_effect=fake_deposit_start,
            ),
            mock.patch.object(supervisor, "stop_program", side_effect=stopped.append),
            mock.patch.object(rpc, "json_rpc_response", side_effect=fake_rpc),
            mock.patch.object(checkpoint, "atomic_write"),
        ):
            validator.generate_deposit_signature(args)

        self.assertEqual(
            calls,
            [
                (
                    "getDepositSignature",
                    [validator.DEPOSIT_AMOUNT_GWEI, validator.WITHDRAWAL_ADDRESS],
                )
            ],
        )
        self.assertEqual(stopped, ["summit-deposit-rpc"])

    def test_deposit_json_rpc_error_fails_without_retrying(self) -> None:
        args = SimpleNamespace(
            inventory=None,
            output=Path("/tmp/deposit-signature.json"),
            overwrite=False,
            startup_timeout=10.0,
            http_timeout=5.0,
        )
        stopped: list[str] = []

        def fake_deposit_start(
            name: str,
            timeout: float,
            *,
            on_start_requested: Callable[[], None],
        ) -> bool:
            on_start_requested()
            return True

        with (
            mock.patch.object(checkpoint, "require_root"),
            mock.patch.object(checkpoint, "load_inventory"),
            mock.patch.object(checkpoint, "require_absolute"),
            mock.patch.object(validator, "confirm_output_overwrite"),
            mock.patch.object(supervisor, "require_stopped"),
            mock.patch.object(
                supervisor,
                "start_program",
                side_effect=fake_deposit_start,
            ),
            mock.patch.object(supervisor, "stop_program", side_effect=stopped.append),
            mock.patch.object(
                rpc,
                "json_rpc_response",
                side_effect=rpc.JsonRpcError(4000, "invalid request"),
            ) as request,
            self.assertRaisesRegex(checkpoint.CheckpointError, "rejected the request"),
        ):
            validator.generate_deposit_signature(args)

        request.assert_called_once()
        self.assertEqual(stopped, ["summit-deposit-rpc"])

    def test_active_validator_is_authorized_with_warning(self) -> None:
        args = SimpleNamespace(
            summit_bearer_token_file=None,
            validator_wait_timeout=0.0,
            pre_joining_policy=None,
            summit_rpc_url="https://network.example/summit",
            http_timeout=10.0,
            validator_poll_interval=0.01,
        )
        account = {
            "status": "Active",
            "balance": 32_000_000_000,
            "joining_epoch": 14,
        }
        stderr = io.StringIO()
        with (
            mock.patch.object(rpc, "read_bearer_token", return_value=None),
            mock.patch.object(validator, "validator_account", return_value=account),
            redirect_stderr(stderr),
        ):
            self.assertTrue(
                validator.wait_for_start_authorization(args, "11" * 32).start
            )
        self.assertIn("already Active", stderr.getvalue())

    def test_exit_state_refuses_startup(self) -> None:
        args = SimpleNamespace(
            summit_bearer_token_file=None,
            validator_wait_timeout=0.0,
            pre_joining_policy="start",
            summit_rpc_url="https://network.example/summit",
            http_timeout=10.0,
            validator_poll_interval=0.01,
        )
        account = {
            "status": "SubmittedExitRequest",
            "balance": 32_000_000_000,
            "joining_epoch": 14,
        }
        with (
            mock.patch.object(rpc, "read_bearer_token", return_value=None),
            mock.patch.object(validator, "validator_account", return_value=account),
            self.assertRaisesRegex(checkpoint.CheckpointError, "Refusing to start"),
        ):
            validator.wait_for_start_authorization(args, "11" * 32)

    def test_explicit_pre_joining_start_is_recorded(self) -> None:
        args = SimpleNamespace(
            summit_bearer_token_file=None,
            validator_wait_timeout=0.0,
            pre_joining_policy="start",
            summit_rpc_url="https://network.example/summit",
            http_timeout=10.0,
            validator_poll_interval=0.01,
        )
        with (
            mock.patch.object(rpc, "read_bearer_token", return_value=None),
            mock.patch.object(validator, "validator_account", return_value=None),
        ):
            decision = validator.wait_for_start_authorization(args, "11" * 32)
        self.assertTrue(decision.start)
        self.assertTrue(decision.pre_joining)

    def test_shared_expired_wait_deadline_fails_without_checkpoint_claim(self) -> None:
        args = SimpleNamespace(
            summit_bearer_token_file=None,
            validator_wait_timeout=30.0,
            pre_joining_policy="wait",
            summit_rpc_url="https://network.example/summit",
            http_timeout=10.0,
            validator_poll_interval=0.01,
        )
        with (
            mock.patch.object(rpc, "read_bearer_token", return_value=None),
            mock.patch.object(validator, "validator_account", return_value=None),
            mock.patch.object(validator.time, "monotonic", return_value=20.0),
            self.assertRaisesRegex(
                checkpoint.CheckpointError,
                "services remain stopped",
            ) as raised,
        ):
            validator.wait_for_start_authorization(
                args,
                "11" * 32,
                wait_deadline=10.0,
            )
        self.assertNotIn("checkpoint remains installed", str(raised.exception))

    def test_pre_joining_leave_stopped_does_not_authorize_start(self) -> None:
        args = SimpleNamespace(
            summit_bearer_token_file=None,
            validator_wait_timeout=0.0,
            pre_joining_policy="leave-stopped",
            summit_rpc_url="https://network.example/summit",
            http_timeout=10.0,
            validator_poll_interval=0.01,
        )
        with (
            mock.patch.object(rpc, "read_bearer_token", return_value=None),
            mock.patch.object(validator, "validator_account", return_value=None),
        ):
            self.assertFalse(
                validator.wait_for_start_authorization(args, "11" * 32).start
            )


class ObserverTests(unittest.TestCase):
    def test_normal_start_ignores_installed_checkpoint_config(self) -> None:
        args = SimpleNamespace(
            inventory=None,
            mode="normal",
            startup_timeout=30.0,
        )
        with tempfile.TemporaryDirectory() as temporary_directory:
            checkpoint_config = Path(temporary_directory) / "observer-checkpoint.toml"
            checkpoint_config.write_text("checkpoint_path = '/unused'\n")
            with (
                mock.patch.dict(
                    checkpoint.CHECKPOINT_CONFIG_PATHS,
                    {"observer": checkpoint_config},
                ),
                mock.patch.object(checkpoint, "load_inventory"),
                mock.patch.object(supervisor, "start_node") as start_node,
            ):
                observer.start_observer(args)
        start_node.assert_called_once_with(
            "summit-observer",
            "summit-observer-checkpoint",
            startup_timeout=30.0,
        )


class SupervisorTests(unittest.TestCase):
    def test_status_accepts_supervisor_not_running_exit_code(self) -> None:
        result = subprocess.CompletedProcess(
            ["supervisorctl", "status", "reth"],
            3,
            stdout="reth STOPPED Not started\n",
            stderr="",
        )
        with mock.patch.object(supervisor, "run_supervisorctl", return_value=result):
            status = supervisor.status("reth")
        self.assertEqual(status.state, "STOPPED")
        self.assertFalse(status.running)

    def test_start_program_accepts_stopped_precheck_exit_code(self) -> None:
        results = iter(
            (
                subprocess.CompletedProcess(
                    ["supervisorctl", "status", "reth"],
                    3,
                    stdout="reth STOPPED Not started\n",
                    stderr="",
                ),
                subprocess.CompletedProcess(
                    ["supervisorctl", "start", "reth"],
                    0,
                    stdout="reth: started\n",
                    stderr="",
                ),
                subprocess.CompletedProcess(
                    ["supervisorctl", "status", "reth"],
                    0,
                    stdout="reth RUNNING pid 123, uptime 0:00:01\n",
                    stderr="",
                ),
            )
        )
        requested: list[str] = []
        with mock.patch.object(
            supervisor,
            "run_supervisorctl",
            side_effect=lambda *arguments: next(results),
        ):
            self.assertTrue(
                supervisor.start_program(
                    "reth",
                    10.0,
                    on_start_requested=lambda: requested.append("reth"),
                )
            )
        self.assertEqual(requested, ["reth"])

    def test_backoff_is_not_a_safe_stopped_state(self) -> None:
        with (
            mock.patch.object(
                supervisor,
                "status",
                return_value=supervisor.ProgramStatus("reth", "BACKOFF", "retrying"),
            ),
            self.assertRaisesRegex(supervisor.SupervisorError, "reth=BACKOFF"),
        ):
            supervisor.require_stopped(("reth",))

    def test_checkpoint_uses_shared_supervisor_state_parser(self) -> None:
        with mock.patch.object(
            supervisor,
            "status",
            return_value=supervisor.ProgramStatus("reth", "STOPPED", ""),
        ):
            self.assertFalse(checkpoint.supervisor_program_running("reth"))
        with mock.patch.object(
            supervisor,
            "status",
            return_value=supervisor.ProgramStatus("reth", "BACKOFF", "retrying"),
        ):
            self.assertTrue(checkpoint.supervisor_program_running("reth"))

    def test_checkpoint_start_order(self) -> None:
        statuses = {
            name: supervisor.ProgramStatus(name, "STOPPED", "")
            for name in (
                "summit",
                "summit-deposit-rpc",
                "reth",
                "summit-checkpoint",
                "custodian",
                "checkpointer",
            )
        }
        started: list[str] = []

        def fake_successful_start(
            name: str,
            timeout: float,
            *,
            on_start_requested: Callable[[], None],
        ) -> bool:
            on_start_requested()
            started.append(name)
            return True

        with (
            mock.patch.object(supervisor, "status", side_effect=statuses.__getitem__),
            mock.patch.object(
                supervisor,
                "start_program",
                side_effect=fake_successful_start,
            ),
        ):
            supervisor.start_node(
                "summit-checkpoint",
                "summit",
                startup_timeout=10.0,
            )
        self.assertEqual(
            started,
            ["custodian", "reth", "summit-checkpoint", "checkpointer"],
        )

    def test_partial_start_failure_stops_only_started_programs_in_reverse(self) -> None:
        statuses = {
            "summit": supervisor.ProgramStatus("summit", "STOPPED", ""),
            "summit-deposit-rpc": supervisor.ProgramStatus(
                "summit-deposit-rpc", "STOPPED", ""
            ),
            "reth": supervisor.ProgramStatus("reth", "STOPPED", ""),
            "summit-checkpoint": supervisor.ProgramStatus(
                "summit-checkpoint", "STOPPED", ""
            ),
            "custodian": supervisor.ProgramStatus("custodian", "STOPPED", ""),
            "checkpointer": supervisor.ProgramStatus(
                "checkpointer", "MISSING", "", False
            ),
        }
        stopped: list[str] = []

        def fake_status(name: str) -> supervisor.ProgramStatus:
            return statuses[name]

        def fake_start(
            name: str,
            timeout: float,
            *,
            on_start_requested: Callable[[], None],
        ) -> bool:
            on_start_requested()
            if name == "summit-checkpoint":
                raise supervisor.SupervisorError("failed")
            return True

        with (
            mock.patch.object(supervisor, "status", side_effect=fake_status),
            mock.patch.object(supervisor, "start_program", side_effect=fake_start),
            mock.patch.object(supervisor, "stop_program", side_effect=stopped.append),
            self.assertRaisesRegex(supervisor.SupervisorError, "failed"),
        ):
            supervisor.start_node(
                "summit-checkpoint",
                "summit",
                startup_timeout=10.0,
            )
        self.assertEqual(stopped, ["summit-checkpoint", "reth", "custodian"])


if __name__ == "__main__":
    unittest.main()

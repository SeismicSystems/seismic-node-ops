"""Tests for checkpoint acquisition and node-onboarding decisions."""

from __future__ import annotations

import argparse
import contextlib
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
        if self.path == "/api/checkpoints/13/manifest":
            self.send_response(200)
            self.send_header("Content-Length", str(len(self.manifest)))
            self.end_headers()
            self.wfile.write(self.manifest)
            return
        if self.path == "/api/checkpoints/13/snapshot":
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

    @staticmethod
    def publish_fixture_manifest() -> None:
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

    def test_remote_checkpoint_and_rpc_anchor_are_resolved(self) -> None:
        self.publish_fixture_manifest()
        args = SimpleNamespace(
            archive=None,
            manifest=None,
            snapshot_api_url=f"{self.base_url}/api",
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

    def test_waiting_manifest_hint_mentions_previous_epoch_once(self) -> None:
        not_found = rpc.HttpStatusError(404, "https://snap.example/manifest")
        responses = iter((not_found, not_found, b"{}"))

        def fake_request(*arguments: object, **options: object) -> bytes:
            value = next(responses)
            if isinstance(value, Exception):
                raise value
            return value

        output = io.StringIO()
        with (
            tempfile.TemporaryDirectory() as temporary_directory,
            mock.patch.object(rpc, "request_bytes", side_effect=fake_request),
            mock.patch.object(checkpoint, "load_manifest", return_value={"epoch": 189}),
            mock.patch.object(download.time, "sleep"),
            contextlib.redirect_stdout(output),
        ):
            manifest = download.fetch_manifest_when_available(
                "https://snap.example/checkpoints/189/manifest",
                Path(temporary_directory) / "manifest.json",
                None,
                interval=0.01,
                deadline=None,
                timeout=1.0,
                unavailable_hint="Rerun with --checkpoint-epoch 188.",
            )
        self.assertEqual(manifest, {"epoch": 189})
        self.assertEqual(
            output.getvalue().count("Rerun with --checkpoint-epoch 188."), 1
        )

    def test_snapshot_urls_use_checkpoint_app_shape(self) -> None:
        manifest_url, archive_url = download.snapshot_urls(
            "https://checkpoint.example/api", 190
        )
        self.assertEqual(
            manifest_url, "https://checkpoint.example/api/checkpoints/190/manifest"
        )
        self.assertEqual(
            archive_url, "https://checkpoint.example/api/checkpoints/190/snapshot"
        )

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
            yes=False,
        )

    def test_install_modifiers_without_source_get_direct_error(self) -> None:
        for attribute, value in (
            ("checkpoint_policy", "exact"),
            ("yes", True),
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
            mock.patch.object(supervisor, "prepare_supervisor") as prepare,
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
        prepare.assert_called_once()

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
            mock.patch.object(supervisor, "prepare_supervisor"),
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

    def test_checkpoint_start_prepares_supervisor_before_programs(self) -> None:
        args = SimpleNamespace(startup_timeout=30.0, inventory=None, mode="checkpoint")
        events: list[str] = []
        with (
            mock.patch.object(
                validator,
                "wait_for_start_authorization",
                return_value=validator.StartDecision(start=True),
            ),
            mock.patch.object(checkpoint, "validate_checkpoint_start_configuration"),
            mock.patch.object(checkpoint, "load_inventory"),
            mock.patch.object(
                validator, "installed_node_public_key", return_value="11" * 32
            ),
            mock.patch.object(
                supervisor,
                "prepare_supervisor",
                side_effect=lambda: events.append("prepare"),
            ) as prepare,
            mock.patch.object(
                supervisor,
                "start_node",
                side_effect=lambda *args, **kwargs: events.append("start"),
            ) as start_node,
        ):
            validator.start_onboarded_validator(
                args,
                "11" * 32,
                allow_pre_joining_start=False,
            )
        self.assertEqual(events, ["prepare", "start"])
        prepare.assert_called_once_with()
        start_node.assert_called_once_with(
            "summit-checkpoint",
            "summit",
            startup_timeout=30.0,
        )

    def test_stop_validates_inventory_and_stops_all_validator_modes(self) -> None:
        args = SimpleNamespace(inventory=None)
        with (
            mock.patch.object(checkpoint, "load_inventory") as load_inventory,
            mock.patch.object(supervisor, "stop_node") as stop_node,
        ):
            validator.stop_validator(args)
        load_inventory.assert_called_once_with(
            "validator", checkpoint.DEFAULT_INVENTORY_PATHS["validator"]
        )
        stop_node.assert_called_once_with(
            ("summit-deposit-rpc", "summit", "summit-checkpoint")
        )

    def test_cli_accepts_validator_stop_without_onboarding_options(self) -> None:
        with mock.patch.object(sys, "argv", ["seismic-node.py", "validator", "stop"]):
            args = node_cli.parse_args()
        self.assertEqual(args.command, "validator")
        self.assertEqual(args.validator_command, "stop")
        self.assertIsNone(args.inventory)


class InstalledValidatorIdentityTests(unittest.TestCase):
    def setUp(self) -> None:
        temporary = tempfile.TemporaryDirectory()
        self.addCleanup(temporary.cleanup)
        self.keys_dir = Path(temporary.name)
        for name in ("node_key.pem", "consensus_key.pem"):
            (self.keys_dir / name).write_text("fixture-only-not-a-real-key")
        self.inventory = {"summit_keys_dir": self.keys_dir}

    def test_read_only_summit_command_returns_normalized_public_key(self) -> None:
        for prefix in ("", "0x"):
            with self.subTest(prefix=prefix):
                output = (
                    f"Node Public Key (ed25519): {prefix}{'AB' * 32}\n"
                    f"Consensus Public Key (BLS): {'CD' * 48}\n"
                )
                with mock.patch.object(
                    validator.subprocess,
                    "run",
                    return_value=subprocess.CompletedProcess([], 0, stdout=output),
                ) as run:
                    public_key = validator.installed_node_public_key(self.inventory)
                self.assertEqual(public_key, "ab" * 32)
                run.assert_called_once_with(
                    [
                        str(validator.SUMMIT),
                        "keys",
                        "show",
                        "--key-store-path",
                        str(self.keys_dir),
                    ],
                    stdin=subprocess.DEVNULL,
                    stdout=subprocess.PIPE,
                    stderr=subprocess.DEVNULL,
                    text=True,
                    check=True,
                    timeout=30.0,
                )

    def test_invalid_or_ambiguous_public_output_is_rejected(self) -> None:
        valid = f"Node Public Key (ed25519): {'ab' * 32}\n"
        for output in (
            "",
            "Consensus Public Key (BLS): " + "ab" * 48,
            "Node Public Key (ed25519): " + "ab" * 31,
            "Node Public Key (ed25519): " + "zz" * 32,
            valid + valid,
            valid.rstrip() + " extra",
            "secret-fixture-not-a-public-key",
        ):
            with (
                self.subTest(output=output),
                mock.patch.object(
                    validator.subprocess,
                    "run",
                    return_value=subprocess.CompletedProcess([], 0, stdout=output),
                ),
                self.assertRaisesRegex(
                    checkpoint.CheckpointError, "exactly one valid"
                ) as raised,
            ):
                validator.installed_node_public_key(self.inventory)
            self.assertNotIn("secret-fixture", str(raised.exception))

    def test_command_failures_do_not_expose_output(self) -> None:
        secret = "secret-fixture-never-print"
        for error in (
            FileNotFoundError(secret),
            subprocess.CalledProcessError(1, ["summit"], output=secret, stderr=secret),
            subprocess.TimeoutExpired(["summit"], 30, output=secret, stderr=secret),
            UnicodeError(secret),
        ):
            with (
                self.subTest(error=type(error).__name__),
                mock.patch.object(validator.subprocess, "run", side_effect=error),
                self.assertRaisesRegex(
                    checkpoint.CheckpointError, "Could not read"
                ) as raised,
            ):
                validator.installed_node_public_key(self.inventory)
            self.assertNotIn(secret, str(raised.exception))

    def test_symlinked_key_directory_is_rejected(self) -> None:
        link = self.keys_dir / "symlink"
        link.symlink_to(self.keys_dir, target_is_directory=True)
        with (
            mock.patch.object(validator.subprocess, "run") as run,
            self.assertRaises(checkpoint.CheckpointError),
        ):
            validator.installed_node_public_key({"summit_keys_dir": link})
        run.assert_not_called()

    def test_missing_empty_and_symlinked_key_files_are_rejected(self) -> None:
        for name in ("node_key.pem", "consensus_key.pem"):
            path = self.keys_dir / name
            for kind in ("missing", "empty", "symlink"):
                with self.subTest(name=name, kind=kind):
                    path.unlink()
                    if kind == "empty":
                        path.touch()
                    elif kind == "symlink":
                        path.symlink_to(self.keys_dir / "missing")
                    with (
                        mock.patch.object(validator.subprocess, "run") as run,
                        self.assertRaises(checkpoint.CheckpointError),
                    ):
                        validator.installed_node_public_key(self.inventory)
                    run.assert_not_called()
                    if path.is_symlink() or path.exists():
                        path.unlink()
                    path.write_text("fixture-only-not-a-real-key")


class ValidatorOnboardTests(unittest.TestCase):
    def setUp(self) -> None:
        self.stack = contextlib.ExitStack()
        self.addCleanup(self.stack.close)
        self.output = io.StringIO()
        self.stack.enter_context(contextlib.redirect_stdout(self.output))
        self.inventory = self.patch(checkpoint, "load_inventory")
        self.identity = self.patch(
            validator, "installed_node_public_key", return_value="11" * 32
        )
        self.deposit = self.patch(
            validator, "load_deposit_response", return_value=({}, "11" * 32)
        )
        self.validate_checkpoint = self.patch(
            checkpoint, "validate_checkpoint_start_configuration"
        )
        self.install = self.patch(node_cli, "install_from_resolved_inputs")
        self.prepare = self.patch(supervisor, "prepare_supervisor")
        self.start = self.patch(supervisor, "start_node")
        self.account = self.patch(
            validator, "validator_account", return_value=self.status("Joining")
        )
        self.sleep = self.patch(validator.time, "sleep")
        self.patch(rpc, "read_bearer_token", return_value=None)

    def patch(self, target: object, name: str, **kwargs: object) -> mock.Mock:
        return self.stack.enter_context(mock.patch.object(target, name, **kwargs))

    @staticmethod
    def status(name: str) -> dict[str, object]:
        return {"status": name, "balance": 32_000_000_000, "joining_epoch": 14}

    def args(self, *options: str) -> argparse.Namespace:
        with mock.patch.object(
            sys,
            "argv",
            [
                "seismic-node.py",
                "validator",
                "onboard",
                "--summit-rpc-url",
                "https://network.example/summit",
                "--pre-joining-policy",
                "wait",
                *options,
            ],
        ):
            return node_cli.parse_args()

    def assert_no_start(self) -> None:
        self.prepare.assert_not_called()
        self.start.assert_not_called()

    def test_normal_waits_for_joining_then_rechecks_before_start(self) -> None:
        self.account.side_effect = [
            None,
            self.status("Inactive"),
            self.status("Joining"),
            self.status("Joining"),
        ]
        events: list[str] = []

        def check_waiting(_: float) -> None:
            self.assert_no_start()
            self.install.assert_not_called()
            events.append("wait")

        self.sleep.side_effect = check_waiting
        self.prepare.side_effect = lambda: events.append("prepare")
        self.start.side_effect = lambda *a, **kw: events.append("start")
        node_cli.handle_validator(
            self.args("--mode", "normal", "--inventory", "/etc/seismic/custom.toml")
        )
        self.assertEqual(events, ["wait", "wait", "prepare", "start"])
        self.assertEqual(self.account.call_count, 4)
        self.inventory.assert_has_calls(
            [mock.call("validator", Path("/etc/seismic/custom.toml"))] * 2
        )
        self.start.assert_called_once_with(
            "summit", "summit-checkpoint", startup_timeout=30.0
        )
        self.validate_checkpoint.assert_not_called()
        self.install.assert_not_called()

    def test_both_modes_use_installed_identity_without_deposit_file(self) -> None:
        for mode in ("normal", "checkpoint"):
            with self.subTest(mode=mode):
                args = self.args("--mode", mode)
                self.assertIsNone(args.deposit_signature)
                self.account.reset_mock()
                self.identity.reset_mock()
                node_cli.handle_validator(args)
                self.assertEqual(self.account.call_count, 2)
                for call in self.account.call_args_list:
                    self.assertEqual(call.args[1], "11" * 32)
                self.assertEqual(self.identity.call_count, 2)
                self.identity.assert_called_with(self.inventory.return_value)
        self.deposit.assert_not_called()

    def test_optional_deposit_response_is_checked_against_installed_identity(
        self,
    ) -> None:
        path = Path("/root/deposit-signature.json")
        node_cli.handle_validator(
            self.args("--mode", "normal", "--deposit-signature", str(path))
        )
        self.deposit.assert_called_once_with(path)
        self.start.assert_called_once()

    def test_invalid_optional_deposit_response_is_not_ignored(self) -> None:
        self.deposit.side_effect = checkpoint.CheckpointError(
            "Invalid deposit response"
        )
        with self.assertRaisesRegex(
            checkpoint.CheckpointError, "Invalid deposit response"
        ):
            node_cli.handle_validator(
                self.args("--deposit-signature", "/root/deposit-signature.json")
            )
        self.account.assert_not_called()
        self.install.assert_not_called()
        self.assert_no_start()

    def test_mismatched_deposit_identity_fails_before_polling_or_install(self) -> None:
        self.deposit.return_value = ({}, "22" * 32)
        with self.assertRaisesRegex(checkpoint.CheckpointError, "does not match"):
            node_cli.handle_validator(
                self.args("--deposit-signature", "/root/deposit-signature.json")
            )
        self.account.assert_not_called()
        self.install.assert_not_called()
        self.assert_no_start()

    def test_identity_read_failure_fails_before_polling_or_install(self) -> None:
        self.identity.side_effect = checkpoint.CheckpointError("Cannot read identity")
        with self.assertRaisesRegex(checkpoint.CheckpointError, "Cannot read identity"):
            node_cli.handle_validator(self.args("--mode", "normal"))
        self.account.assert_not_called()
        self.install.assert_not_called()
        self.assert_no_start()

    def test_key_change_while_waiting_refuses_start_in_both_modes(self) -> None:
        for mode in ("normal", "checkpoint"):
            with self.subTest(mode=mode):
                self.identity.side_effect = ["11" * 32, "22" * 32]
                with self.assertRaisesRegex(
                    checkpoint.CheckpointError, "changed during"
                ):
                    node_cli.handle_validator(self.args("--mode", mode))
                self.assert_no_start()

    def test_checkpoint_remains_default_and_uses_existing_inputs(self) -> None:
        args = self.args()
        self.assertEqual(args.mode, "checkpoint")
        node_cli.handle_validator(args)
        self.validate_checkpoint.assert_has_calls([mock.call("validator")] * 2)
        self.install.assert_not_called()
        self.start.assert_called_once_with(
            "summit-checkpoint", "summit", startup_timeout=30.0
        )

    def test_checkpoint_download_follows_authorization(self) -> None:
        self.account.side_effect = [
            None,
            self.status("Joining"),
            self.status("Joining"),
        ]
        self.sleep.side_effect = lambda _: self.install.assert_not_called()
        self.install.side_effect = lambda *a, **kw: self.assert_no_start()
        node_cli.handle_validator(
            self.args("--snapshot-api-url", "https://snapshot.example")
        )
        self.install.assert_called_once()
        self.start.assert_called_once_with(
            "summit-checkpoint", "summit", startup_timeout=30.0
        )

    def test_normal_rejects_checkpoint_sources_and_install_modifiers(self) -> None:
        for options in (
            ["--archive", "/tmp/archive"],
            ["--manifest", "/tmp/manifest"],
            ["--snapshot-api-url", "https://snapshot.example"],
            ["--snapshot-bearer-token-file", "/root/token"],
            ["--checkpoint-epoch", "14"],
            ["--checkpoint-policy", "exact"],
            ["--weak-subjectivity-path", "/tmp/anchor"],
            ["--weak-subjectivity-rpc-url", "https://anchor.example"],
            ["--checkpoint-path", "/persistence/checkpoint"],
            ["--backup-root", "/persistence/backup"],
            ["--installed-weak-subjectivity-path", "/etc/seismic/custom-anchor"],
            ["--allow-same-origin-weak-subjectivity"],
            ["--yes"],
        ):
            with (
                self.subTest(options=options),
                self.assertRaisesRegex(checkpoint.CheckpointError, "--mode normal"),
            ):
                node_cli.handle_validator(self.args("--mode", "normal", *options))
        self.account.assert_not_called()
        self.inventory.assert_not_called()
        self.install.assert_not_called()
        self.assert_no_start()

    def test_normal_active_starts_with_warning(self) -> None:
        self.account.return_value = self.status("Active")
        stderr = io.StringIO()
        with redirect_stderr(stderr):
            node_cli.handle_validator(self.args("--mode", "normal"))
        self.assertIn("already Active", stderr.getvalue())
        self.start.assert_called_once()

    def test_normal_refuses_unsafe_status_on_either_check(self) -> None:
        for status in ("SubmittedExitRequest", "FullPayoutPending", "Unknown"):
            for initial_joining in (False, True):
                with self.subTest(status=status, initial_joining=initial_joining):
                    self.account.side_effect = (
                        [self.status("Joining"), self.status(status)]
                        if initial_joining
                        else [self.status(status)]
                    )
                    with self.assertRaisesRegex(checkpoint.CheckpointError, "Refusing"):
                        node_cli.handle_validator(self.args("--mode", "normal"))
                    self.assert_no_start()
        self.install.assert_not_called()

    def test_normal_leave_stopped_on_either_check(self) -> None:
        for initial_joining in (False, True):
            with self.subTest(initial_joining=initial_joining):
                self.account.side_effect = (
                    [self.status("Joining"), None] if initial_joining else [None]
                )
                node_cli.handle_validator(
                    self.args(
                        "--mode", "normal", "--pre-joining-policy", "leave-stopped"
                    )
                )
                self.assert_no_start()
        self.assertIn("No checkpoint was installed", self.output.getvalue())
        self.assertNotIn("checkpoint remains installed", self.output.getvalue().lower())
        self.install.assert_not_called()

    def test_normal_preserves_interactive_early_start_authorization(self) -> None:
        args = self.args("--mode", "normal")
        args.pre_joining_policy = None
        self.account.return_value = None
        with mock.patch.object(
            validator, "choose_pre_joining_action", return_value="start"
        ) as choose:
            node_cli.handle_validator(args)
        choose.assert_called_once_with("NotFound", None)
        self.assertEqual(self.account.call_count, 2)
        self.start.assert_called_once()

    def test_normal_shared_wait_deadline_applies_to_second_check(self) -> None:
        self.account.side_effect = [self.status("Joining"), None]
        with (
            mock.patch.object(validator.time, "monotonic", side_effect=[10.0, 12.0]),
            self.assertRaisesRegex(checkpoint.CheckpointError, "Timed out"),
        ):
            node_cli.handle_validator(
                self.args("--mode", "normal", "--validator-wait-timeout", "1")
            )
        self.assert_no_start()
        self.install.assert_not_called()

    def test_normal_waits_again_if_status_regresses_before_startup(self) -> None:
        self.account.side_effect = [
            self.status("Joining"),
            self.status("Inactive"),
            self.status("Joining"),
        ]
        self.sleep.side_effect = lambda _: self.assert_no_start()
        node_cli.handle_validator(self.args("--mode", "normal"))
        self.sleep.assert_called_once()
        self.assertEqual(self.account.call_count, 3)
        self.start.assert_called_once()

    def test_normal_rpc_error_never_starts_services(self) -> None:
        self.account.side_effect = checkpoint.CheckpointError("Invalid RPC response")
        with self.assertRaisesRegex(checkpoint.CheckpointError, "Invalid RPC response"):
            node_cli.handle_validator(self.args("--mode", "normal"))
        self.assert_no_start()
        self.install.assert_not_called()

    def test_normal_invalid_inventory_fails_before_polling(self) -> None:
        self.inventory.side_effect = checkpoint.CheckpointError("Invalid inventory")
        with self.assertRaisesRegex(checkpoint.CheckpointError, "Invalid inventory"):
            node_cli.handle_validator(self.args("--mode", "normal"))
        self.account.assert_not_called()
        self.assert_no_start()

    def test_normal_requires_trusted_rpc(self) -> None:
        args = self.args("--mode", "normal")
        args.summit_rpc_url = None
        with self.assertRaisesRegex(checkpoint.CheckpointError, "--summit-rpc-url"):
            node_cli.handle_validator(args)
        self.account.assert_not_called()
        self.assert_no_start()

    def test_checkpoint_startup_failure_still_reports_rollback(self) -> None:
        backup = Path("/persistence/rollback/fixture")
        self.install.return_value = backup
        self.start.side_effect = supervisor.SupervisorError("Startup failed")
        with (
            mock.patch.object(node_cli, "print_startup_rollback") as rollback,
            self.assertRaisesRegex(supervisor.SupervisorError, "Startup failed"),
        ):
            node_cli.handle_validator(
                self.args("--snapshot-api-url", "https://snapshot.example")
            )
        rollback.assert_called_once_with(backup)

    def test_normal_startup_failure_has_no_checkpoint_rollback(self) -> None:
        self.start.side_effect = supervisor.SupervisorError("Startup failed")
        with (
            mock.patch.object(node_cli, "print_startup_rollback") as rollback,
            self.assertRaisesRegex(supervisor.SupervisorError, "Startup failed"),
        ):
            node_cli.handle_validator(self.args("--mode", "normal"))
        rollback.assert_not_called()
        self.install.assert_not_called()


class ValidatorStartTests(unittest.TestCase):
    def start_args(self, mode: str) -> SimpleNamespace:
        return SimpleNamespace(inventory=None, mode=mode, startup_timeout=30.0)

    def test_normal_start_ignores_installed_checkpoint_config(self) -> None:
        with (
            mock.patch.object(checkpoint, "load_inventory"),
            mock.patch.object(supervisor, "prepare_supervisor"),
            mock.patch.object(supervisor, "start_node") as start_node,
        ):
            validator.start_validator(self.start_args("normal"))
        start_node.assert_called_once_with(
            "summit",
            "summit-checkpoint",
            startup_timeout=30.0,
        )

    def test_checkpoint_start_validates_installed_configuration(self) -> None:
        with (
            mock.patch.object(checkpoint, "load_inventory"),
            mock.patch.object(
                checkpoint, "validate_checkpoint_start_configuration"
            ) as validate,
            mock.patch.object(supervisor, "prepare_supervisor"),
            mock.patch.object(supervisor, "start_node") as start_node,
        ):
            validator.start_validator(self.start_args("checkpoint"))
        validate.assert_called_once_with("validator")
        start_node.assert_called_once_with(
            "summit-checkpoint",
            "summit",
            startup_timeout=30.0,
        )


class ObserverTests(unittest.TestCase):
    def test_normal_start_ignores_installed_checkpoint_config(self) -> None:
        args = SimpleNamespace(
            inventory=None,
            mode="normal",
            startup_timeout=30.0,
        )
        events: list[str] = []
        with tempfile.TemporaryDirectory() as temporary_directory:
            checkpoint_config = Path(temporary_directory) / "observer-checkpoint.toml"
            checkpoint_config.write_text("checkpoint_path = '/unused'\n")
            with (
                mock.patch.dict(
                    checkpoint.CHECKPOINT_CONFIG_PATHS,
                    {"observer": checkpoint_config},
                ),
                mock.patch.object(checkpoint, "load_inventory"),
                mock.patch.object(
                    supervisor,
                    "prepare_supervisor",
                    side_effect=lambda: events.append("prepare"),
                ) as prepare,
                mock.patch.object(
                    supervisor,
                    "start_node",
                    side_effect=lambda *args, **kwargs: events.append("start"),
                ) as start_node,
            ):
                observer.start_observer(args)
        self.assertEqual(events, ["prepare", "start"])
        prepare.assert_called_once_with()
        start_node.assert_called_once_with(
            "summit-observer",
            "summit-observer-checkpoint",
            startup_timeout=30.0,
        )

    def test_stop_validates_inventory_and_stops_both_observer_modes(self) -> None:
        args = SimpleNamespace(inventory=None)
        with (
            mock.patch.object(checkpoint, "load_inventory") as load_inventory,
            mock.patch.object(supervisor, "stop_node") as stop_node,
        ):
            observer.stop_observer(args)
        load_inventory.assert_called_once_with(
            "observer", checkpoint.DEFAULT_INVENTORY_PATHS["observer"]
        )
        stop_node.assert_called_once_with(
            ("summit-observer", "summit-observer-checkpoint")
        )

    def test_cli_accepts_observer_stop_without_mode(self) -> None:
        with mock.patch.object(sys, "argv", ["seismic-node.py", "observer", "stop"]):
            args = node_cli.parse_args()
        self.assertEqual(args.command, "observer")
        self.assertEqual(args.observer_command, "stop")
        self.assertIsNone(args.inventory)


class SupervisorTests(unittest.TestCase):
    def test_prepare_supervisor_enables_rereads_and_updates(self) -> None:
        success = subprocess.CompletedProcess([], 0, stdout="", stderr="")
        with (
            mock.patch.object(
                supervisor, "run_systemctl", return_value=success
            ) as systemctl,
            mock.patch.object(
                supervisor, "run_supervisorctl", return_value=success
            ) as supervisorctl,
        ):
            supervisor.prepare_supervisor()
        systemctl.assert_called_once_with("enable", "--now", "supervisor")
        self.assertEqual(
            supervisorctl.call_args_list,
            [mock.call("reread"), mock.call("update")],
        )

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

    def test_stop_node_uses_reverse_dependency_order(self) -> None:
        requested: list[str] = []

        def fake_stop(name: str) -> bool:
            requested.append(name)
            return name != "summit-observer-checkpoint"

        with mock.patch.object(supervisor, "stop_program", side_effect=fake_stop):
            stopped = supervisor.stop_node(
                ("summit-observer", "summit-observer-checkpoint")
            )
        self.assertEqual(
            requested,
            [
                "checkpointer",
                "summit-observer",
                "summit-observer-checkpoint",
                "reth",
                "custodian",
            ],
        )
        self.assertEqual(
            stopped,
            ["checkpointer", "summit-observer", "reth", "custodian"],
        )

    def test_stop_failure_preserves_lower_level_dependencies(self) -> None:
        requested: list[str] = []

        def fake_stop(name: str) -> bool:
            requested.append(name)
            if name == "summit-observer":
                raise supervisor.SupervisorError("failed")
            return True

        with (
            mock.patch.object(supervisor, "stop_program", side_effect=fake_stop),
            self.assertRaisesRegex(supervisor.SupervisorError, "failed"),
        ):
            supervisor.stop_node(("summit-observer", "summit-observer-checkpoint"))
        self.assertEqual(requested, ["checkpointer", "summit-observer"])

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

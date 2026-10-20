"""Tests for the systemd service-control backend."""

from __future__ import annotations

import subprocess
import sys
import unittest
from pathlib import Path
from unittest import mock

ROOT = Path(__file__).resolve().parents[1]
sys.path.insert(0, str(ROOT / "tools"))

from seismic_node import systemd


def show(load: str, active: str, sub: str = "", result: str = "success"):
    """Fake ``systemctl show`` output."""
    return subprocess.CompletedProcess(
        [],
        0,
        stdout=f"LoadState={load}\nActiveState={active}\nSubState={sub}\nResult={result}\n",
        stderr="",
    )


OK = subprocess.CompletedProcess([], 0, stdout="", stderr="")


class FakeSystemctl:
    """Scripted systemctl that records calls and answers ``show`` from state."""

    def __init__(self, states: dict[str, str]):
        self.states = dict(states)
        self.calls: list[tuple[str, ...]] = []
        self.fail_start: set[str] = set()

    def __call__(self, *arguments: str) -> subprocess.CompletedProcess[str]:
        self.calls.append(arguments)
        verb = arguments[0]
        if verb == "show":
            unit = arguments[1]
            state = self.states.get(unit)
            if state is None:
                return show("not-found", "inactive", "dead")
            return show("loaded", state, "running" if state == "active" else "dead")
        unit = arguments[1] if len(arguments) > 1 else ""
        if verb == "start":
            if unit in self.fail_start:
                self.states[unit] = "failed"
                return subprocess.CompletedProcess(
                    [], 1, stdout="", stderr=f"Job for {unit} failed."
                )
            self.states[unit] = "active"
        elif verb == "stop" or verb == "reset-failed":
            self.states[unit] = "inactive"
        return OK

    def units(self, verb: str) -> list[str]:
        return [c[1] for c in self.calls if c[0] == verb]


class StatusTests(unittest.TestCase):
    def test_maps_program_names_to_units(self):
        self.assertEqual(
            systemd.unit_for("summit-checkpoint"), "seismic-summit-checkpoint.service"
        )
        self.assertEqual(
            systemd.unit_for("summit-deposit-rpc"), "seismic-deposit-rpc.service"
        )
        with self.assertRaisesRegex(systemd.ServiceError, "Unknown node program"):
            systemd.unit_for("nginx")

    def test_parses_states(self):
        cases = {
            "active": ("active", True, True),
            "inactive": ("inactive", False, True),
            "failed": ("failed", False, True),
            "activating": ("activating", False, True),
        }
        for active, (state, running, exists) in cases.items():
            with (
                self.subTest(active=active),
                mock.patch.object(
                    systemd, "run_systemctl", return_value=show("loaded", active)
                ),
            ):
                value = systemd.status("reth")
                self.assertEqual(
                    (value.state, value.running, value.exists), (state, running, exists)
                )

    def test_missing_unit(self):
        with mock.patch.object(
            systemd, "run_systemctl", return_value=show("not-found", "inactive")
        ):
            value = systemd.status("checkpointer")
        self.assertFalse(value.exists)
        self.assertEqual(value.state, "missing")

    def test_failed_includes_result(self):
        with mock.patch.object(
            systemd,
            "run_systemctl",
            return_value=show("loaded", "failed", "failed", "exit-code"),
        ):
            self.assertEqual(systemd.status("reth").detail, "failed (exit-code)")

    def test_rejects_unexpected_output(self):
        for result in (
            show("loaded", "weird"),
            show("masked", "inactive"),
            subprocess.CompletedProcess([], 1, stdout="", stderr="boom"),
        ):
            with (
                self.subTest(result=result),
                mock.patch.object(systemd, "run_systemctl", return_value=result),
                self.assertRaises(systemd.ServiceError),
            ):
                systemd.status("reth")

    def test_require_stopped_rejects_transient_and_running(self):
        for state in ("active", "activating", "deactivating"):
            with (
                self.subTest(state=state),
                mock.patch.object(
                    systemd, "run_systemctl", return_value=show("loaded", state)
                ),
                self.assertRaisesRegex(systemd.ServiceError, f"reth={state}"),
            ):
                systemd.require_stopped(("reth",))
        with mock.patch.object(
            systemd, "run_systemctl", return_value=show("loaded", "failed")
        ):
            systemd.require_stopped(("reth",))

    def test_require_stopped_missing_policy(self):
        with mock.patch.object(
            systemd, "run_systemctl", return_value=show("not-found", "inactive")
        ):
            systemd.require_stopped(("checkpointer",))
            with self.assertRaisesRegex(systemd.ServiceError, "checkpointer=missing"):
                systemd.require_stopped(("checkpointer",), allow_missing=False)


class LifecycleTests(unittest.TestCase):
    def test_start_issues_start_and_confirms_active(self):
        fake = FakeSystemctl({"seismic-reth.service": "inactive"})
        requested: list[str] = []
        with mock.patch.object(systemd, "run_systemctl", side_effect=fake):
            self.assertTrue(
                systemd.start_service(
                    "reth", on_start_requested=lambda: requested.append("reth")
                )
            )
        self.assertEqual(fake.units("start"), ["seismic-reth.service"])
        self.assertEqual(requested, ["reth"])
        self.assertEqual(fake.units("reset-failed"), [])

    def test_start_is_noop_when_already_active(self):
        fake = FakeSystemctl({"seismic-reth.service": "active"})
        with mock.patch.object(systemd, "run_systemctl", side_effect=fake):
            self.assertFalse(systemd.start_service("reth"))
        self.assertEqual(fake.units("start"), [])

    def test_start_resets_failed_marker_first(self):
        fake = FakeSystemctl({"seismic-reth.service": "failed"})
        with mock.patch.object(systemd, "run_systemctl", side_effect=fake):
            systemd.start_service("reth")
        self.assertEqual(
            [c[0] for c in fake.calls], ["show", "reset-failed", "start", "show"]
        )

    def test_start_refuses_transient_state(self):
        fake = FakeSystemctl({"seismic-reth.service": "activating"})
        with (
            mock.patch.object(systemd, "run_systemctl", side_effect=fake),
            self.assertRaisesRegex(systemd.ServiceError, "while it is activating"),
        ):
            systemd.start_service("reth")

    def test_start_failure_reports_state_and_keeps_callback_ordering(self):
        fake = FakeSystemctl({"seismic-reth.service": "inactive"})
        fake.fail_start.add("seismic-reth.service")
        requested: list[str] = []
        with (
            mock.patch.object(systemd, "run_systemctl", side_effect=fake),
            self.assertRaisesRegex(
                systemd.ServiceError, "reth failed during startup: failed"
            ),
        ):
            systemd.start_service(
                "reth", on_start_requested=lambda: requested.append("reth")
            )
        self.assertEqual(
            requested, ["reth"], "callback must run before the start attempt"
        )

    def test_stop_only_when_running(self):
        fake = FakeSystemctl(
            {"seismic-reth.service": "active", "seismic-custodian.service": "inactive"}
        )
        with mock.patch.object(systemd, "run_systemctl", side_effect=fake):
            self.assertTrue(systemd.stop_service("reth"))
            self.assertFalse(systemd.stop_service("custodian"))
            self.assertFalse(systemd.stop_service("checkpointer"))
        self.assertEqual(fake.units("stop"), ["seismic-reth.service"])

    def test_reload_units(self):
        with mock.patch.object(systemd, "run_systemctl", return_value=OK) as run:
            systemd.reload_units()
        run.assert_called_once_with("daemon-reload")


class NodeOrchestrationTests(unittest.TestCase):
    def test_validator_checkpoint_start_order(self):
        fake = FakeSystemctl(
            {
                f"seismic-{u}.service": "inactive"
                for u in (
                    "reth",
                    "summit",
                    "summit-checkpoint",
                    "deposit-rpc",
                    "custodian",
                    "checkpointer",
                )
            }
        )
        with mock.patch.object(systemd, "run_systemctl", side_effect=fake):
            started = systemd.start_node("summit-checkpoint", "summit")
        self.assertEqual(
            started, ["custodian", "reth", "summit-checkpoint", "checkpointer"]
        )
        self.assertEqual(
            fake.units("start"),
            [
                "seismic-custodian.service",
                "seismic-reth.service",
                "seismic-summit-checkpoint.service",
                "seismic-checkpointer.service",
            ],
        )

    def test_observer_uses_observer_custodian_unit(self):
        fake = FakeSystemctl(
            {
                f"seismic-{u}.service": "inactive"
                for u in (
                    "reth",
                    "observer",
                    "observer-checkpoint",
                    "observer-custodian",
                )
            }
        )
        with mock.patch.object(systemd, "run_systemctl", side_effect=fake):
            started = systemd.start_node(
                "summit-observer",
                "summit-observer-checkpoint",
                custodian_program="observer-custodian",
            )
        self.assertEqual(started, ["observer-custodian", "reth", "summit-observer"])

    def test_start_refuses_when_conflicting_summit_runs(self):
        fake = FakeSystemctl(
            {
                "seismic-summit.service": "active",
                "seismic-deposit-rpc.service": "inactive",
            }
        )
        with (
            mock.patch.object(systemd, "run_systemctl", side_effect=fake),
            self.assertRaisesRegex(systemd.ServiceError, "summit=active"),
        ):
            systemd.start_node("summit-checkpoint", "summit")
        self.assertEqual(fake.units("start"), [])

    def test_partial_start_failure_stops_only_started_in_reverse(self):
        fake = FakeSystemctl(
            {
                f"seismic-{u}.service": "inactive"
                for u in (
                    "reth",
                    "summit",
                    "summit-checkpoint",
                    "deposit-rpc",
                    "custodian",
                )
            }
        )
        fake.fail_start.add("seismic-summit-checkpoint.service")
        with (
            mock.patch.object(systemd, "run_systemctl", side_effect=fake),
            self.assertRaisesRegex(
                systemd.ServiceError, "summit-checkpoint failed during startup"
            ),
        ):
            systemd.start_node("summit-checkpoint", "summit")
        # The failed unit is already quiescent (failed is a stopped state), so
        # only the units this command actually brought up are stopped.
        self.assertEqual(
            fake.units("stop"),
            ["seismic-reth.service", "seismic-custodian.service"],
        )

    def test_already_running_services_are_not_stopped_on_failure(self):
        fake = FakeSystemctl(
            {
                "seismic-custodian.service": "active",
                "seismic-reth.service": "active",
                "seismic-summit.service": "inactive",
                "seismic-summit-checkpoint.service": "inactive",
                "seismic-deposit-rpc.service": "inactive",
            }
        )
        fake.fail_start.add("seismic-summit.service")
        with (
            mock.patch.object(systemd, "run_systemctl", side_effect=fake),
            self.assertRaises(systemd.ServiceError),
        ):
            systemd.start_node("summit", "summit-checkpoint")
        self.assertEqual(fake.units("stop"), [])
        self.assertEqual(fake.states["seismic-reth.service"], "active")
        self.assertEqual(fake.states["seismic-custodian.service"], "active")

    def test_stop_node_reverse_order_and_result(self):
        fake = FakeSystemctl(
            {
                "seismic-checkpointer.service": "active",
                "seismic-observer.service": "active",
                "seismic-observer-checkpoint.service": "inactive",
                "seismic-reth.service": "active",
                "seismic-observer-custodian.service": "active",
            }
        )
        with mock.patch.object(systemd, "run_systemctl", side_effect=fake):
            stopped = systemd.stop_node(
                ("summit-observer", "summit-observer-checkpoint"),
                custodian_program="observer-custodian",
            )
        self.assertEqual(
            stopped, ["checkpointer", "summit-observer", "reth", "observer-custodian"]
        )
        self.assertEqual(
            fake.units("stop"),
            [
                "seismic-checkpointer.service",
                "seismic-observer.service",
                "seismic-reth.service",
                "seismic-observer-custodian.service",
            ],
        )

    def test_stop_failure_preserves_lower_level_dependencies(self):
        requested: list[str] = []

        def fake_stop(name: str) -> bool:
            requested.append(name)
            if name == "summit-observer":
                raise systemd.ServiceError("failed")
            return True

        with (
            mock.patch.object(systemd, "stop_service", side_effect=fake_stop),
            self.assertRaisesRegex(systemd.ServiceError, "failed"),
        ):
            systemd.stop_node(("summit-observer", "summit-observer-checkpoint"))
        self.assertEqual(requested, ["checkpointer", "summit-observer"])


class CommandTests(unittest.TestCase):
    def test_run_systemctl_requires_binary_and_no_prompt(self):
        with (
            mock.patch.object(Path, "is_file", return_value=False),
            self.assertRaisesRegex(systemd.ServiceError, "systemctl is required"),
        ):
            systemd.run_systemctl("show", "x")
        with (
            mock.patch.object(Path, "is_file", return_value=True),
            mock.patch.object(subprocess, "run", return_value=OK) as run,
        ):
            systemd.run_systemctl("start", "x.service")
        self.assertEqual(
            run.call_args.args[0],
            ["/usr/bin/systemctl", "--no-ask-password", "start", "x.service"],
        )

    def test_require_command_success(self):
        systemd.require_command_success(OK, "x")
        with self.assertRaisesRegex(systemd.ServiceError, "Reloading failed: boom"):
            systemd.require_command_success(
                subprocess.CompletedProcess([], 1, stdout="", stderr="boom"),
                "Reloading",
            )


if __name__ == "__main__":
    unittest.main()

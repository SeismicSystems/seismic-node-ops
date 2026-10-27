"""The operator-loadable alert rules and dashboard under monitoring/.

Both must only name series that docs/METRICS.md documents, and the rules
must carry no Seismic-side routing. With promtool on PATH (or named by
PROMETHEUS_AGENT_PROMTOOL, as in CI) the rules are checked and their unit
tests run, and every dashboard query is parsed.
"""

from __future__ import annotations

import json
import os
import re
import shutil
import subprocess
import tempfile
import unittest
from pathlib import Path

ROOT = Path(__file__).resolve().parents[1]
RULES = ROOT / "monitoring/alerts/seismic-node.yml"
RULE_TESTS = ROOT / "monitoring/alerts/seismic-node.test.yml"
DASHBOARD = ROOT / "monitoring/dashboards/seismic-node.json"
METRICS_DOC = ROOT / "docs/METRICS.md"
PROMTOOL = os.environ.get("PROMETHEUS_AGENT_PROMTOOL") or shutil.which("promtool")

# Every series a node exports starts with one of these; recording rules
# (prefix:name) and PromQL functions do not match.
SERIES = re.compile(
    r"\b((?:summit|reth|custodian|checkpointer|prometheus_remote_storage)_[a-z0-9_]+)"
)
ALERT_NAME = re.compile(r"^\s*- alert: (\w+)", re.MULTILINE)


def documented_series() -> set[str]:
    return set(SERIES.findall(METRICS_DOC.read_text()))


class OperatorRulesTests(unittest.TestCase):
    text = RULES.read_text()

    def test_every_series_is_documented_in_metrics_md(self):
        documented = documented_series()
        for name in sorted(set(SERIES.findall(self.text))):
            with self.subTest(series=name):
                self.assertIn(name, documented)

    def test_no_seismic_routing_only_severity(self):
        # Where an alert goes is the operator's Alertmanager's decision.
        self.assertNotIn("notification:", self.text)
        self.assertNotIn("pagerduty", self.text.lower())
        self.assertNotIn("slack", self.text.lower())
        self.assertNotIn("__", self.text)  # no unrendered deploy placeholders
        for block in re.split(r"(?m)^\s*- alert: ", self.text)[1:]:
            name = block.split()[0]
            with self.subTest(alert=name):
                self.assertRegex(block, r"severity: (critical|warning|none)")

    def test_fleet_layer_is_absent_and_per_node_alerts_present(self):
        for fleet_only in (
            "ValidatorFleetCritical",
            "validator:affected",
            "validator:blackbox_failure_active",
            "validator:progress_stall_active",
            "validator:key_delivery_failure_active",
            "validator:telemetry_missing_active",
            "summit:critical_error_recent_by_node",
            "monitoring:observer",
        ):
            self.assertNotIn(fleet_only, self.text)
        self.assertEqual(
            ALERT_NAME.findall(self.text),
            [
                "SummitDown",
                "RethDown",
                "CustodianDown",
                "CheckpointerDown",
                "NodeTelemetryMissing",
                "SummitStalled",
                "RethStalled",
                "RethNoPeers",
                "RethMissingRotationKeys",
                "CustodianSocketFailures",
                "CustodianCouncilRejections",
                "SummitCriticalError",
                "SummitByzantineFault",
                "SummitWarningError",
                "CheckpointerStale",
                "CheckpointerNoCheckpoint",
                "DiskSpaceLow",
                "AgentRemoteWriteFailing",
                "AgentRemoteWriteLagging",
                "Watchdog",
            ],
        )
        # The one recording rule each event alert is defined on.
        self.assertIn("record: summit:critical_error_event_by_node_reason", self.text)
        self.assertIn("record: summit:warning_error_event_by_node_reason", self.text)

    def test_missing_telemetry_inventory_is_self_maintained(self):
        """Seismic renders the expected streams from its node inventory; the
        operator file has no inventory to render from, so the expected set
        must learn from arriving samples and latch on its own last value."""
        self.assertIn("record: monitoring:expected_push_stream", self.text)
        self.assertIn("record: monitoring:push_stream_missing", self.text)
        expected = re.search(
            r"record: monitoring:expected_push_stream\n\s+expr: \|\n(.*?)\n\n",
            self.text,
            re.DOTALL,
        ).group(1)
        self.assertIn(
            'present_over_time(up{job=~"(summit|reth|custodian|prometheus-agent)/.*"}[2m])',
            expected,
        )
        self.assertNotIn("checkpointer", expected)
        self.assertIn("max by (node, job) (monitoring:expected_push_stream)", expected)
        # The missing check and the alert match the central push rules.
        self.assertIn(
            "monitoring:expected_push_stream\n          unless on (node, job)\n"
            "          present_over_time(up[2m])",
            self.text,
        )
        self.assertIn(
            "expr: max by (node) (monitoring:push_stream_missing) > 0", self.text
        )

    def test_thresholds_are_the_central_defaults(self):
        for expected in (
            'up{job=~"summit/.*"} == 0',
            "increase(summit_blocks_committed_total[2m]) == 0",
            "changes(reth_blockchain_tree_canonical_chain_height[2m]) == 0",
            "reth_seismic_rotation_pending_unfetched_epochs > 0",
            "> 3600",
            "for: 1h",
            "< 0.15",
            "8 * 1024 * 1024 * 1024",
            "for: 15m",
        ):
            self.assertIn(expected, self.text)

    @unittest.skipUnless(PROMTOOL, "promtool is not installed")
    def test_promtool_check_and_unit_tests(self):
        check = subprocess.run(
            [PROMTOOL, "check", "rules", str(RULES)],
            capture_output=True,
            text=True,
            timeout=60,
            check=False,
        )
        self.assertEqual(check.returncode, 0, check.stdout + check.stderr)
        run = subprocess.run(
            [PROMTOOL, "test", "rules", RULE_TESTS.name],
            cwd=RULE_TESTS.parent,
            capture_output=True,
            text=True,
            timeout=120,
            check=False,
        )
        self.assertEqual(run.returncode, 0, run.stdout + run.stderr)


class OperatorDashboardTests(unittest.TestCase):
    @classmethod
    def setUpClass(cls):
        cls.dashboard = json.loads(DASHBOARD.read_text())
        cls.panels = [p for p in cls.dashboard["panels"] if p["type"] != "row"]
        cls.targets = [(p, t) for p in cls.panels for t in p["targets"]]

    def test_identity_and_variables(self):
        self.assertEqual(self.dashboard["uid"], "seismic-node")
        self.assertEqual(self.dashboard["title"], "Seismic node")
        variables = {v["name"]: v for v in self.dashboard["templating"]["list"]}
        self.assertEqual(set(variables), {"datasource", "node"})
        self.assertEqual(variables["datasource"]["type"], "datasource")
        node = variables["node"]
        self.assertTrue(node["multi"] and node["includeAll"])
        self.assertEqual(node["allValue"], ".*")
        self.assertEqual(node["definition"], "label_values(up, node)")
        ids = [p["id"] for p in self.dashboard["panels"]]
        self.assertEqual(len(ids), len(set(ids)))

    def test_every_query_is_node_scoped_and_documented(self):
        documented = documented_series()
        for panel, target in self.targets:
            with self.subTest(panel=panel["title"], ref=target["refId"]):
                self.assertIn('node=~"$node"', target["expr"])
                self.assertEqual(target["datasource"]["uid"], "${datasource}")
                for name in SERIES.findall(target["expr"]):
                    self.assertIn(name, documented)

    def test_descriptions_name_the_alert_that_covers_the_panel(self):
        alerts = set(ALERT_NAME.findall(RULES.read_text()))
        named = {
            name
            for panel in self.panels
            for name in re.findall(r"\b[A-Z][A-Za-z]+\b", panel["description"])
            if name in alerts
        }
        # Every alert except the agent's own watchdog is visible on a panel.
        self.assertEqual(alerts - named, {"Watchdog"})

    def test_rates_are_not_applied_to_gauges(self):
        gauges = (
            "reth_blockchain_tree_canonical_chain_height",
            "reth_network_connected_peers",
            "reth_seismic_rotation_pending_unfetched_epochs",
            "custodian_delivered_epoch",
            "checkpointer_last_checkpoint_timestamp_seconds",
            "summit_disk_available_bytes",
        )
        for _, target in self.targets:
            for gauge in gauges:
                self.assertNotRegex(target["expr"], rf"(rate|increase)\({gauge}")

    @unittest.skipUnless(PROMTOOL, "promtool is not installed")
    def test_every_query_parses(self):
        rules = [
            {
                "record": f"dashboard_{panel['id']}_{target['refId']}",
                "expr": target["expr"].replace("$node", ".*"),
            }
            for panel, target in self.targets
        ]
        with tempfile.TemporaryDirectory(prefix="operator-dashboard-") as tmp:
            path = Path(tmp) / "rules.yml"
            # JSON is valid YAML, so no YAML dependency is needed.
            path.write_text(json.dumps({"groups": [{"name": "d", "rules": rules}]}))
            result = subprocess.run(
                [PROMTOOL, "check", "rules", str(path)],
                capture_output=True,
                text=True,
                timeout=60,
                check=False,
            )
            self.assertEqual(result.returncode, 0, result.stdout + result.stderr)


if __name__ == "__main__":
    unittest.main()

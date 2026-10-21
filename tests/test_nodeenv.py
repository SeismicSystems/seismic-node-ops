"""Tests for node.env parsing and schema validation."""

from __future__ import annotations

import os
import stat
import sys
import tempfile
import unittest
from pathlib import Path

ROOT = Path(__file__).resolve().parents[1]
TOOLS = ROOT / "tools"
sys.path.insert(0, str(TOOLS))

from seismic_node import nodeenv

EXAMPLE = ROOT / "install/templates/node.env.example"


class ParseTests(unittest.TestCase):
    """Each case mirrors behaviour observed from systemd's EnvironmentFile=."""

    def test_comments_blank_lines_and_whitespace(self):
        text = "# comment\n; also\n\n  KEY  =  value  \nTAB=\ttabbed\n"
        self.assertEqual(nodeenv.parse(text), {"KEY": "value", "TAB": "tabbed"})

    def test_hash_inside_value_is_not_a_comment(self):
        self.assertEqual(nodeenv.parse("K=value # kept"), {"K": "value # kept"})

    def test_bare_backslash_escapes_next_character(self):
        self.assertEqual(
            nodeenv.parse('A=a\\b\\\\c\nB=say \\"hi\\"\nC=x\\ny'),
            {"A": "ab\\c", "B": 'say "hi"', "C": "xny"},
        )

    def test_bare_trailing_backslash_continues_without_separator(self):
        self.assertEqual(
            nodeenv.parse("A=first \\\nsecond\nB=first\\\nsecond\nC=end\\"),
            {"A": "first second", "B": "firstsecond", "C": "end"},
        )

    def test_double_quotes_escape_only_quote_and_backslash(self):
        text = 'K="a \\"q\\" \\\\ back \\n literal"'
        self.assertEqual(nodeenv.parse(text), {"K": 'a "q" \\ back \\n literal'})

    def test_double_quotes_span_lines(self):
        self.assertEqual(
            nodeenv.parse('K="line one\nline two"\nN=1'),
            {"K": "line one\nline two", "N": "1"},
        )

    def test_single_quotes_are_literal(self):
        self.assertEqual(
            nodeenv.parse("K='a\\\\b \"dq\" \\n'\nM='x\ny'"),
            {"K": 'a\\\\b "dq" \\n', "M": "x\ny"},
        )

    def test_empty_values(self):
        self.assertEqual(nodeenv.parse("A=\nB=\"\"\nC=''"), {"A": "", "B": "", "C": ""})

    def test_rejects_invalid_keys(self):
        for text in ("BAD KEY=1", "3BAD=1", "K-X=1", "export K=1", "=v"):
            with (
                self.subTest(text=text),
                self.assertRaisesRegex(nodeenv.NodeEnvError, "line 1"),
            ):
                nodeenv.parse(text)

    def test_rejects_missing_equals(self):
        with self.assertRaisesRegex(nodeenv.NodeEnvError, "KEY=value"):
            nodeenv.parse("OK=1\nNOEQ")

    def test_rejects_duplicate_keys(self):
        with self.assertRaisesRegex(nodeenv.NodeEnvError, "line 2: duplicate"):
            nodeenv.parse("K=1\nK=2")

    def test_rejects_text_after_closing_quote(self):
        with self.assertRaisesRegex(nodeenv.NodeEnvError, "after closing quote"):
            nodeenv.parse('K="a" b')

    def test_rejects_unterminated_quotes(self):
        for text in ('K="open\nN=1', "K='open"):
            with (
                self.subTest(text=text),
                self.assertRaisesRegex(nodeenv.NodeEnvError, "unterminated"),
            ):
                nodeenv.parse(text)


# The example leaves network-specific values blank; fill the one required
# key so the example validates as a minimal validator.
REQUIRED = {"CUSTODIAN_CHAIN_ID": "5124"}


def example_values(**overrides: str) -> dict[str, str]:
    values = nodeenv.parse(EXAMPLE.read_text())
    values.update(REQUIRED)
    values.update(overrides)
    return values


OBSERVER = {
    "SEISMIC_ROLE": "observer",
    "OBSERVER_PARENT_NODE_PUBLIC_KEY": "ab" * 32,
    "OBSERVER_INDEX": "3",
    "OBSERVER_PUBLIC_ADDRESS": "203.0.113.10:18551",
    "OBSERVER_P2P_PORT": "18551",
}


class SchemaTests(unittest.TestCase):
    def test_example_validates_as_minimal_validator(self):
        env = nodeenv.validate(example_values())
        self.assertEqual(env.role, "validator")
        self.assertEqual(env.path("RETH_DATA_DIR"), Path("/persistence/reth"))
        self.assertIsNone(env.optional_path("SUMMIT_BOOTSTRAPPERS_FILE"))
        self.assertEqual(env.path("CUSTODIAN_DATA_DIR"), Path("/persistence/custodian"))
        self.assertFalse(env.applies("CUSTODIAN_PARENT_URL"))

    def test_example_validates_with_every_feature_enabled(self):
        env = nodeenv.validate(
            example_values(
                **OBSERVER,
                CUSTODIAN_PARENT_URL="https://parent.example/custodian",
                CHECKPOINTER_ENABLED="true",
                TLS_MODE="full",
                DOMAIN="node.example.com",
                MONITORING_ENABLED="true",
                MONITORING_NODE_NAME="node-1",
                MONITORING_REMOTE_WRITE_URL="https://metrics.example/api/v1/write",
            )
        )
        self.assertEqual(env.integer("OBSERVER_INDEX"), 3)
        self.assertEqual(env.integer("CUSTODIAN_COUNCIL_PORT"), 7876)
        self.assertEqual(env.path("CHECKPOINTS_DIR"), Path("/persistence/checkpoints"))
        self.assertEqual(env.string("DOMAIN"), "node.example.com")

    def test_example_lists_every_schema_key_and_nothing_else(self):
        self.assertEqual(set(example_values()), set(nodeenv.SCHEMA))

    def test_unknown_key_is_rejected(self):
        with self.assertRaisesRegex(nodeenv.NodeEnvError, "unknown keys: TYPO"):
            nodeenv.validate(example_values(TYPO="1"))

    def test_missing_required_key_is_rejected(self):
        values = example_values()
        del values["GENESIS_PATH"]
        with self.assertRaisesRegex(
            nodeenv.NodeEnvError, "missing required keys: GENESIS_PATH"
        ):
            nodeenv.validate(values)

    def test_missing_condition_input_is_rejected(self):
        values = example_values()
        del values["SEISMIC_ROLE"]
        with self.assertRaisesRegex(
            nodeenv.NodeEnvError, "missing required key SEISMIC_ROLE"
        ):
            nodeenv.validate(values)

    def test_observer_keys_required_only_for_observers(self):
        with self.assertRaisesRegex(
            nodeenv.NodeEnvError, "OBSERVER_PARENT_NODE_PUBLIC_KEY"
        ):
            nodeenv.validate(example_values(SEISMIC_ROLE="observer"))
        env = nodeenv.validate(example_values(OBSERVER_PARENT_NODE_PUBLIC_KEY="zz"))
        self.assertFalse(env.applies("OBSERVER_PARENT_NODE_PUBLIC_KEY"))

    def test_custodian_is_always_required(self):
        values = example_values()
        del values["CUSTODIAN_CHAIN_ID"]
        with self.assertRaisesRegex(nodeenv.NodeEnvError, "CUSTODIAN_CHAIN_ID"):
            nodeenv.validate(values)
        self.assertNotIn("CUSTODIAN_ENABLED", nodeenv.SCHEMA)
        self.assertNotIn("RETH_PURPOSE_KEYS_SOURCE", nodeenv.SCHEMA)

    def test_gated_keys_are_required_when_enabled(self):
        with self.assertRaisesRegex(nodeenv.NodeEnvError, "CUSTODIAN_PARENT_URL"):
            nodeenv.validate(example_values(**OBSERVER))
        with self.assertRaisesRegex(nodeenv.NodeEnvError, "DOMAIN"):
            nodeenv.validate(example_values(TLS_MODE="custodian-only"))
        with self.assertRaisesRegex(nodeenv.NodeEnvError, "CUSTODIAN_PUBLIC_URL"):
            nodeenv.validate(example_values(TLS_MODE="external"))
        with self.assertRaisesRegex(nodeenv.NodeEnvError, "MONITORING_NODE_NAME"):
            nodeenv.validate(example_values(MONITORING_ENABLED="true"))

    def test_prebuilt_path_required_only_for_prebuilt_method(self):
        with self.assertRaisesRegex(nodeenv.NodeEnvError, "SUMMIT_PREBUILT_PATH"):
            nodeenv.validate(example_values(SUMMIT_INSTALL_METHOD="prebuilt"))
        env = nodeenv.validate(
            example_values(
                SUMMIT_INSTALL_METHOD="prebuilt", SUMMIT_PREBUILT_PATH="/opt/summit"
            )
        )
        self.assertEqual(env.path("SUMMIT_PREBUILT_PATH"), Path("/opt/summit"))

    def test_type_errors(self):
        cases = {
            "SEISMIC_ROLE": "admin",
            "CHECKPOINTER_ENABLED": "yes",
            "RETH_DATA_DIR": "relative/path",
            "SUMMIT_DATA_DIR": "/persistence/../summit",
            "RETH_RPC_BIND": "localhost",
            "TLS_MODE": "nginx",
            "GENESIS_PATH": "",
        }
        for key, value in cases.items():
            with (
                self.subTest(key=key),
                self.assertRaisesRegex(nodeenv.NodeEnvError, key),
            ):
                nodeenv.validate(example_values(**{key: value}))

    def test_gated_type_errors(self):
        cases = {
            "CUSTODIAN_COUNCIL_PORT": "70000",
            "CUSTODIAN_COUNCIL_ADDRESS": "0x1234",
            "CUSTODIAN_CHAIN_ID": "0",
        }
        for key, value in cases.items():
            with (
                self.subTest(key=key),
                self.assertRaisesRegex(nodeenv.NodeEnvError, key),
            ):
                nodeenv.validate(example_values(**{key: value}))

    def test_derived_keys_must_match_their_sources(self):
        enode = "enode://ab@203.0.113.1:30303"
        cases = {
            "bootnode without arg": {"RETH_BOOTNODE": enode},
            "arg without bootnode": {"RETH_BOOTNODES_ARG": f"--bootnodes {enode}"},
            "arg with wrong bootnode": {
                "RETH_BOOTNODE": enode,
                "RETH_BOOTNODES_ARG": "--bootnodes enode://cd@203.0.113.2:30303",
            },
            "bootstrappers without arg": {
                "SUMMIT_BOOTSTRAPPERS_FILE": "/persistence/keys/summit/bootstrappers.toml"
            },
            "observer port mismatch": {**OBSERVER, "OBSERVER_P2P_PORT": "18552"},
        }
        for name, overrides in cases.items():
            with self.subTest(case=name), self.assertRaises(nodeenv.NodeEnvError):
                nodeenv.validate(example_values(**overrides))

        env = nodeenv.validate(
            example_values(
                RETH_BOOTNODE=enode,
                RETH_BOOTNODES_ARG=f"--bootnodes {enode}",
                SUMMIT_BOOTSTRAPPERS_FILE="/persistence/keys/summit/bootstrappers.toml",
                SUMMIT_BOOTSTRAPPERS_ARG="--bootstrappers /persistence/keys/summit/bootstrappers.toml",
            )
        )
        self.assertEqual(
            env.string("RETH_BOOTNODES_ARG").split(), ["--bootnodes", enode]
        )

    def test_fixed_constants(self):
        self.assertEqual(nodeenv.SERVICE_USER, "seismic")
        self.assertEqual(
            nodeenv.CHECKPOINT_START_CONFIG, Path("/etc/seismic/checkpoint-start.toml")
        )
        self.assertNotIn("SERVICE_USER", nodeenv.SCHEMA)
        self.assertNotIn("RETH_BIN", nodeenv.SCHEMA)
        self.assertTrue(all(p.is_absolute() for p in nodeenv.BINARIES.values()))
        self.assertNotIn("CHECKPOINT_START_CONFIG", nodeenv.SCHEMA)

    def test_observer_type_errors(self):
        cases = {
            "OBSERVER_PARENT_NODE_PUBLIC_KEY": "AB" * 32,
            "OBSERVER_INDEX": "-1",
            "OBSERVER_PUBLIC_ADDRESS": "203.0.113.10",
        }
        for key, value in cases.items():
            with (
                self.subTest(key=key),
                self.assertRaisesRegex(nodeenv.NodeEnvError, key),
            ):
                nodeenv.validate(example_values(**{**OBSERVER, key: value}))

    def test_layout_rules(self):
        cases = {
            "nested data dirs": {"SUMMIT_DATA_DIR": "/persistence/reth/summit"},
            "identical data dirs": {"SUMMIT_DATA_DIR": "/persistence/reth"},
            "p2p key outside keys dir": {"RETH_P2P_KEY_PATH": "/persistence/p2p"},
            "summit keys outside keys dir": {"SUMMIT_KEYS_DIR": "/persistence/skeys"},
            "critical log outside summit data": {
                "SUMMIT_CRITICAL_LOG_DIR": "/var/log/critical"
            },
            "p2p key inside summit keys": {
                "RETH_P2P_KEY_PATH": "/persistence/keys/summit/p2p"
            },
            "bootstrappers outside summit keys": {
                "SUMMIT_BOOTSTRAPPERS_FILE": "/persistence/keys/bootstrappers.toml"
            },
            "checkpoints inside reth data": {
                "CHECKPOINTER_ENABLED": "true",
                "CHECKPOINTS_DIR": "/persistence/reth/checkpoints",
            },
            "monitoring inside summit data": {
                "MONITORING_ENABLED": "true",
                "MONITORING_NODE_NAME": "n",
                "MONITORING_REMOTE_WRITE_URL": "https://m.example/api/v1/write",
                "MONITORING_DATA_DIR": "/persistence/summit/agent",
            },
        }
        for name, overrides in cases.items():
            with self.subTest(case=name), self.assertRaises(nodeenv.NodeEnvError):
                nodeenv.validate(example_values(**overrides))

    def test_accessor_type_guards(self):
        env = nodeenv.validate(example_values())
        with self.assertRaisesRegex(nodeenv.NodeEnvError, "not a path"):
            env.path("SEISMIC_ROLE")
        with self.assertRaisesRegex(nodeenv.NodeEnvError, "not a boolean"):
            env.flag("SEISMIC_ROLE")
        with self.assertRaisesRegex(nodeenv.NodeEnvError, "not an integer"):
            env.integer("CHECKPOINTER_ENABLED")
        with self.assertRaisesRegex(nodeenv.NodeEnvError, "does not apply"):
            env.get("CUSTODIAN_PARENT_URL")


class LoadTests(unittest.TestCase):
    def setUp(self):
        temporary = tempfile.TemporaryDirectory()
        self.addCleanup(temporary.cleanup)
        self.directory = Path(temporary.name)

    def write(self, text: str, mode: int = 0o640) -> Path:
        path = self.directory / "node.env"
        path.write_text(text)
        path.chmod(mode)
        return path

    @unittest.skipUnless(os.geteuid() == 0, "root ownership checks need root")
    def test_load_as_root(self):
        path = self.write(EXAMPLE.read_text())
        env = nodeenv.load(path)
        self.assertEqual(env.source, path)
        self.assertEqual(env.role, "validator")

    @unittest.skipIf(os.geteuid() == 0, "non-root ownership is rejected")
    def test_load_rejects_files_not_owned_by_root(self):
        path = self.write(EXAMPLE.read_text())
        with self.assertRaisesRegex(nodeenv.NodeEnvError, "root-owned"):
            nodeenv.load(path)

    def test_load_rejects_group_writable_file(self):
        path = self.write(EXAMPLE.read_text(), mode=0o660)
        with self.assertRaisesRegex(nodeenv.NodeEnvError, "writable"):
            nodeenv.load(path)
        self.assertTrue(stat.S_IMODE(path.stat().st_mode) & 0o020)

    def test_load_rejects_missing_file(self):
        with self.assertRaisesRegex(nodeenv.NodeEnvError, "missing"):
            nodeenv.load(self.directory / "absent.env")

    def test_parse_errors_carry_through_load(self):
        path = self.write("BAD KEY=1")
        with self.assertRaises(nodeenv.NodeEnvError):
            nodeenv.load(path)


if __name__ == "__main__":
    unittest.main()

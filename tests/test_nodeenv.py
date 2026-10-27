"""Tests for node.env parsing and schema validation."""

from __future__ import annotations

import contextlib
import io
import os
import re
import shutil
import stat
import subprocess
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

    def test_double_quotes_unescape_the_shell_set_and_keep_other_backslashes(self):
        # systemd's DOUBLE_QUOTE_VALUE_ESCAPE: \" \\ \$ \` lose the backslash,
        # anything else keeps it. Values are what `systemd-run -p
        # EnvironmentFile=` showed on systemd 262.
        cases = {
            'K="a \\"q\\" \\\\ back \\n literal"': 'a "q" \\ back \\n literal',
            'M="a\\$b"': "a$b",
            'N="a\\`b"': "a`b",
            'O="\\t\\x\\ "': "\\t\\x\\ ",
            'P="$unescaped `stays`"': "$unescaped `stays`",
        }
        for text, expected in cases.items():
            with self.subTest(text=text):
                self.assertEqual(list(nodeenv.parse(text).values()), [expected])

    def test_double_quotes_eat_backslash_newline(self):
        self.assertEqual(
            nodeenv.parse('A="foo\\\nbar"\nB="one \\\n two\\\nthree"\nC=1'),
            {"A": "foobar", "B": "one  twothree", "C": "1"},
        )
        # A backslash before a literal newline is not the same as before a
        # quote: an escaped quote at end of line does not close the value.
        self.assertEqual(
            nodeenv.parse('A="x\\"\ny"'),
            {"A": 'x"\ny'},
        )
        with self.assertRaisesRegex(nodeenv.NodeEnvError, 'unterminated " quote'):
            nodeenv.parse('A="foo\\')

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


# The example leaves network-specific values blank; fill the required keys so
# the example validates as a minimal validator (push monitoring is the default).
REQUIRED = {
    "CUSTODIAN_CHAIN_ID": "5124",
    "DOMAIN": "node.example.com",
    "MONITORING_NODE_NAME": "node-1",
    "MONITORING_REMOTE_WRITE_URL": "https://metrics.example/api/v1/write",
}


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
            nodeenv.validate(example_values(DOMAIN=""))
        with self.assertRaisesRegex(nodeenv.NodeEnvError, "CUSTODIAN_PUBLIC_URL"):
            nodeenv.validate(example_values(TLS_MODE="external"))
        with self.assertRaisesRegex(nodeenv.NodeEnvError, "MONITORING_NODE_NAME"):
            nodeenv.validate(example_values(MONITORING_NODE_NAME=""))
        with self.assertRaisesRegex(
            nodeenv.NodeEnvError, "MONITORING_REMOTE_WRITE_URL"
        ):
            nodeenv.validate(example_values(MONITORING_REMOTE_WRITE_URL=""))

    def test_monitoring_mode(self):
        for bad in ("true", "false", "agent"):
            with (
                self.subTest(bad),
                self.assertRaisesRegex(nodeenv.NodeEnvError, "MONITORING_MODE"),
            ):
                nodeenv.validate(example_values(MONITORING_MODE=bad))
        # The MONITORING_* settings belong to the agent, so pull ignores them.
        pull = nodeenv.validate(
            example_values(
                MONITORING_MODE="pull",
                TLS_MODE="full",
                DOMAIN="node.example.com",
                MONITORING_NODE_NAME="",
                MONITORING_REMOTE_WRITE_URL="",
            )
        )
        self.assertEqual(pull.monitoring_mode, "pull")
        self.assertFalse(pull.applies("MONITORING_REMOTE_WRITE_URL"))
        external = nodeenv.validate(
            example_values(
                MONITORING_MODE="pull",
                TLS_MODE="external",
                CUSTODIAN_PUBLIC_URL="https://node.example.com/custodian",
            )
        )
        self.assertEqual(external.monitoring_mode, "pull")
        # The operator's proxy must enforce the same JWT check as the managed
        # /prom-* routes, so pull behind it still needs the secret.
        self.assertTrue(external.applies("OPENRESTY_JWT_SECRET_PATH"))
        self.assertFalse(
            nodeenv.validate(
                example_values(
                    MONITORING_MODE="push",
                    TLS_MODE="external",
                    CUSTODIAN_PUBLIC_URL="https://node.example.com/custodian",
                )
            ).applies("OPENRESTY_JWT_SECRET_PATH")
        )
        # Publishing metrics does not require publishing the node's RPC.
        custodian_only = nodeenv.validate(
            example_values(
                MONITORING_MODE="pull",
                TLS_MODE="custodian-only",
                DOMAIN="node.example.com",
            )
        )
        self.assertEqual(custodian_only.monitoring_mode, "pull")
        # Pull needs some HTTPS proxy on the node. TLS_MODE=disabled is only
        # valid for observers, and observers cannot pull, so the role rules
        # reject the combination before the proxy rule is reached.
        with self.assertRaisesRegex(nodeenv.NodeEnvError, "only valid for observers"):
            nodeenv.validate(
                example_values(MONITORING_MODE="pull", TLS_MODE="disabled")
            )
        self.assertEqual(nodeenv.PULL_TLS_MODES, ("custodian-only", "full", "external"))

    def test_disabled_tls_is_for_observers_only(self):
        observer = nodeenv.validate(
            example_values(
                **OBSERVER,
                CUSTODIAN_PARENT_URL="https://parent.example/custodian",
                TLS_MODE="disabled",
                DOMAIN="",
            )
        )
        self.assertEqual(observer.string("TLS_MODE"), "disabled")
        self.assertFalse(observer.applies("DOMAIN"))
        with self.assertRaisesRegex(nodeenv.NodeEnvError, "only valid for observers"):
            nodeenv.validate(example_values(TLS_MODE="disabled"))
        # The example is a validator and therefore exposes the Custodian.
        self.assertEqual(
            nodeenv.validate(example_values()).string("TLS_MODE"), "custodian-only"
        )
        # Push is the validator default; the example leaves the key empty.
        self.assertEqual(example_values()["MONITORING_MODE"], "")
        # Off is accepted and needs none of the agent's keys.
        self.assertEqual(nodeenv.validate(example_values()).monitoring_mode, "push")
        off = nodeenv.validate(
            example_values(
                MONITORING_MODE="off",
                MONITORING_NODE_NAME="",
                MONITORING_REMOTE_WRITE_URL="",
            )
        )
        self.assertEqual(off.monitoring_mode, "off")
        self.assertFalse(off.applies("MONITORING_NODE_NAME"))

    def test_observer_monitoring(self):
        observer = dict(
            OBSERVER, CUSTODIAN_PARENT_URL="https://parent.example/custodian"
        )
        # The empty key resolves to off for observers: no agent keys needed.
        env = nodeenv.validate(example_values(**observer))
        self.assertEqual(env.monitoring_mode, "off")
        self.assertFalse(env.applies("MONITORING_REMOTE_WRITE_URL"))
        # Push is allowed, but only to the operator's own Prometheus: Seismic's
        # URL does not apply and the operator's is required.
        with self.assertRaisesRegex(nodeenv.NodeEnvError, "never push to Seismic"):
            nodeenv.validate(example_values(**observer, MONITORING_MODE="push"))
        env = nodeenv.validate(
            example_values(
                **observer,
                MONITORING_MODE="push",
                MONITORING_OPERATOR_REMOTE_WRITE_URL="https://prom.op.example/api/v1/write",
            )
        )
        self.assertEqual(env.monitoring_mode, "push")
        self.assertFalse(env.applies("MONITORING_REMOTE_WRITE_URL"))
        self.assertEqual(env.string("MONITORING_NODE_NAME"), "node-1")
        # Seismic does not scrape observers.
        with self.assertRaisesRegex(nodeenv.NodeEnvError, "not available to observers"):
            nodeenv.validate(
                example_values(
                    **observer,
                    MONITORING_MODE="pull",
                    TLS_MODE="full",
                    DOMAIN="node.example.com",
                )
            )

    def test_supported_install_methods_for_every_component(self):
        for component in ("SUMMIT", "RETH", "CUSTODIAN", "CHECKPOINTER"):
            for method in ("download", "source", "prebuilt"):
                with self.subTest(component=component, method=method):
                    key = f"{component}_INSTALL_METHOD"
                    env = nodeenv.validate(
                        example_values(
                            CHECKPOINTER_ENABLED="true",
                            RETH_MDBX_COPY_PREBUILT_PATH="/opt/mdbx_copy",
                            **{
                                key: method,
                                f"{component}_PREBUILT_PATH": f"/opt/{component.lower()}",
                            },
                        )
                    )
                    self.assertEqual(env.string(key), method)
                    self.assertEqual(
                        env.applies(f"{component}_PREBUILT_PATH"), method == "prebuilt"
                    )

    def test_deferred_install_method_is_rejected_for_every_component(self):
        for component in ("SUMMIT", "RETH", "CUSTODIAN", "CHECKPOINTER"):
            key = f"{component}_INSTALL_METHOD"
            with (
                self.subTest(component=component),
                self.assertRaisesRegex(
                    nodeenv.NodeEnvError,
                    f"{key} must be one of: download, prebuilt, source$",
                ),
            ):
                nodeenv.validate(
                    example_values(CHECKPOINTER_ENABLED="true", **{key: "deferred"})
                )

    def test_prebuilt_path_required_only_for_prebuilt_method(self):
        with self.assertRaisesRegex(nodeenv.NodeEnvError, "SUMMIT_PREBUILT_PATH"):
            nodeenv.validate(example_values(SUMMIT_INSTALL_METHOD="prebuilt"))
        env = nodeenv.validate(
            example_values(
                SUMMIT_INSTALL_METHOD="prebuilt", SUMMIT_PREBUILT_PATH="/opt/summit"
            )
        )
        self.assertEqual(env.path("SUMMIT_PREBUILT_PATH"), Path("/opt/summit"))

    def test_prebuilt_reth_with_checkpointer_needs_matching_mdbx_copy_path(self):
        prebuilt = {
            "RETH_INSTALL_METHOD": "prebuilt",
            "RETH_PREBUILT_PATH": "/opt/seismic-reth",
        }
        # Without the checkpointer, Reth is the only prebuilt binary.
        env = nodeenv.validate(example_values(**prebuilt))
        self.assertFalse(env.applies("RETH_MDBX_COPY_PREBUILT_PATH"))
        # download/source derive mdbx_copy from Reth's release or checkout.
        for method in ("download", "source"):
            env = nodeenv.validate(
                example_values(CHECKPOINTER_ENABLED="true", RETH_INSTALL_METHOD=method)
            )
            self.assertFalse(env.applies("RETH_MDBX_COPY_PREBUILT_PATH"))
        with self.assertRaisesRegex(
            nodeenv.NodeEnvError, r"RETH_MDBX_COPY_PREBUILT_PATH must not be empty$"
        ):
            nodeenv.validate(example_values(CHECKPOINTER_ENABLED="true", **prebuilt))
        values = example_values(CHECKPOINTER_ENABLED="true", **prebuilt)
        del values["RETH_MDBX_COPY_PREBUILT_PATH"]
        with self.assertRaisesRegex(
            nodeenv.NodeEnvError,
            r"missing required keys: RETH_MDBX_COPY_PREBUILT_PATH$",
        ):
            nodeenv.validate(values)
        with self.assertRaisesRegex(
            nodeenv.NodeEnvError, "must name mdbx_copy, not the Reth binary"
        ):
            nodeenv.validate(
                example_values(
                    CHECKPOINTER_ENABLED="true",
                    RETH_MDBX_COPY_PREBUILT_PATH="/opt/seismic-reth",
                    **prebuilt,
                )
            )
        env = nodeenv.validate(
            example_values(
                CHECKPOINTER_ENABLED="true",
                RETH_MDBX_COPY_PREBUILT_PATH="/opt/mdbx_copy",
                **prebuilt,
            )
        )
        self.assertEqual(
            env.path("RETH_MDBX_COPY_PREBUILT_PATH"), Path("/opt/mdbx_copy")
        )

    def test_example_lists_the_options_of_every_closed_choice_key(self):
        """Every key whose validator accepts a closed set of values has a
        `# a | b | c` line in the comment block directly above it in
        node.env.example, naming exactly the accepted values (MONITORING_MODE
        may add `empty` for the role default)."""
        text = EXAMPLE.read_text().splitlines()
        closed = {
            key: field.validate.choices
            for key, field in nodeenv.SCHEMA.items()
            if hasattr(field.validate, "choices")
        }
        self.assertEqual(
            set(closed),
            {
                "SEISMIC_ROLE",
                "SUMMIT_INSTALL_METHOD",
                "RETH_INSTALL_METHOD",
                "CUSTODIAN_INSTALL_METHOD",
                "CHECKPOINTER_ENABLED",
                "CHECKPOINTER_INSTALL_METHOD",
                "TLS_MODE",
                "MONITORING_MODE",
            },
        )
        for key, choices in closed.items():
            with self.subTest(key=key):
                index = next(
                    i for i, line in enumerate(text) if line.startswith(f"{key}=")
                )
                block = []
                while index > 0 and text[index - 1].startswith("#"):
                    index -= 1
                    block.insert(0, text[index])
                listed = None
                for line in block:
                    match = re.fullmatch(
                        r"# ([a-z-]+(?: \| [a-z-]+)+)(?:\s+\(.*)?", line
                    )
                    if match:
                        listed = {part.strip() for part in match.group(1).split("|")}
                        break
                self.assertIsNotNone(
                    listed, f"{key}: no `# a | b` options line above it"
                )
                extra = {"empty"} if key == "MONITORING_MODE" else set()
                self.assertEqual(listed - extra, set(choices))

    def test_type_errors(self):
        cases = {
            "SEISMIC_ROLE": "admin",
            "CHECKPOINTER_ENABLED": "yes",
            "RETH_DATA_DIR": "relative/path",
            "SUMMIT_DATA_DIR": "/persistence/../summit",
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
        enode = "enode://" + "ab" * 64 + "@203.0.113.1:30303"
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

    def test_versions_are_release_tags_or_branch_refs(self):
        for good in ("v0.1.0", "v1.7.0-seismic.1", "v0.2.0-rc.1"):
            with self.subTest(version=good):
                nodeenv.validate(example_values(SUMMIT_VERSION=good))
        for bad in ("0.1.0", "internal-testnet-v1", "refs/tags/v0.1.0", "main", ""):
            with (
                self.subTest(version=bad),
                self.assertRaisesRegex(nodeenv.NodeEnvError, "SUMMIT_VERSION"),
            ):
                nodeenv.validate(example_values(SUMMIT_VERSION=bad))

    def test_branch_refs_only_for_source_builds(self):
        env = nodeenv.validate(
            example_values(
                RETH_INSTALL_METHOD="source", RETH_VERSION="refs/heads/seismic"
            )
        )
        self.assertEqual(env.string("RETH_VERSION"), "refs/heads/seismic")
        with self.assertRaisesRegex(nodeenv.NodeEnvError, "not downloadable"):
            nodeenv.validate(example_values(RETH_VERSION="refs/heads/seismic"))

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

    def test_observer_public_address_is_a_socket_address(self):
        """Summit parses --ip with Rust's SocketAddr and panics on anything
        else, so a hostname must not pass --check."""
        for value in (
            "203.0.113.10:18551",
            "0.0.0.0:18551",
            "[2001:db8::10]:18551",
            "[::]:1",
        ):
            with self.subTest(value=value):
                env = nodeenv.validate(
                    example_values(
                        **{
                            **OBSERVER,
                            "CUSTODIAN_PARENT_URL": "https://parent.example/custodian",
                            "OBSERVER_PUBLIC_ADDRESS": value,
                            "OBSERVER_P2P_PORT": value.rpartition(":")[2],
                        }
                    )
                )
                self.assertEqual(env.string("OBSERVER_PUBLIC_ADDRESS"), value)
        for value in (
            "node.example.com:18551",
            "localhost:18551",
            "2001:db8::10:18551",  # IPv6 needs brackets
            "[2001:db8::10]",
            "[203.0.113.10]:18551",
            "[fe80::1%eth0]:18551",
            "203.0.113.010:18551",
            "203.0.113.10:0",
            "203.0.113.10:65536",
            "203.0.113.10:",
            ":18551",
        ):
            with (
                self.subTest(value=value),
                self.assertRaisesRegex(
                    nodeenv.NodeEnvError,
                    r"OBSERVER_PUBLIC_ADDRESS must be IPv4:port or \[IPv6\]:port",
                ),
            ):
                nodeenv.validate(
                    example_values(**{**OBSERVER, "OBSERVER_PUBLIC_ADDRESS": value})
                )

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
                "MONITORING_MODE": "push",
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


def bash_eval(exports: str, keys: list[str]) -> dict[str, str]:
    """Eval the export block in a real bash and read the variables back."""
    script = 'eval "$1"; shift; for k; do printf "%s\\0" "${!k}"; done'
    result = subprocess.run(
        ["bash", "-euo", "pipefail", "-c", script, "bash", exports, *keys],
        capture_output=True,
        check=True,
        cwd="/",
    )
    values = result.stdout.decode().split("\0")
    assert values.pop() == ""
    return dict(zip(keys, values, strict=True))


@unittest.skipUnless(shutil.which("bash"), "bash not available")
class ShellExportTests(unittest.TestCase):
    """The installer must see the values this parser validated, not bash's."""

    def test_bash_reproduces_every_value_byte_for_byte(self):
        # Each of these is valid EnvironmentFile= syntax that bash would read
        # differently or execute if the file were sourced.
        values = example_values(
            RETH_DATA_DIR="/persistence/reth # fast disk",
            SUMMIT_DATA_DIR="/persistence/$HOSTNAME/summit",
            SUMMIT_CRITICAL_LOG_DIR="/persistence/$HOSTNAME/summit/critical",
            MONITORING_NODE_NAME='$(id -u) it\'s \\ "q" `x` ;&|',
        )
        env = nodeenv.validate(values)
        seen = bash_eval(nodeenv.shell_exports(env), list(values))
        self.assertEqual(seen, env.raw)
        self.assertEqual(seen["RETH_DATA_DIR"], "/persistence/reth # fast disk")
        self.assertEqual(seen["SUMMIT_DATA_DIR"], "/persistence/$HOSTNAME/summit")
        self.assertEqual(seen["MONITORING_NODE_NAME"], '$(id -u) it\'s \\ "q" `x` ;&|')

    def test_sourcing_the_same_file_would_differ(self):
        # Documents the gap the export closes: bash drops the inline comment
        # and expands the substitution when it sources the raw file.
        values = example_values(
            RETH_DATA_DIR="/persistence/reth # fast disk",
            MONITORING_NODE_NAME="$(id -u)",
        )
        nodeenv.validate(values)
        text = "".join(f"{key}={value}\n" for key, value in values.items())
        with tempfile.TemporaryDirectory() as directory:
            path = Path(directory) / "node.env"
            path.write_text(text)
            sourced = bash_eval(
                f"source {path}", ["RETH_DATA_DIR", "MONITORING_NODE_NAME"]
            )
        self.assertEqual(sourced["RETH_DATA_DIR"], "/persistence/reth")
        self.assertEqual(sourced["MONITORING_NODE_NAME"], str(os.getuid()))

    def test_exports_are_assignments_only(self):
        env = nodeenv.validate(example_values())
        for line in nodeenv.shell_exports(env).splitlines():
            self.assertRegex(
                line, r"^export [A-Z][A-Z0-9_]*=('.*'|[A-Za-z0-9_./:@-]*)$"
            )

    def test_blank_monitoring_mode_is_exported_resolved(self):
        observer = dict(
            OBSERVER, CUSTODIAN_PARENT_URL="https://parent.example/custodian"
        )
        for role, expected in (({}, "push"), (observer, "off")):
            env = nodeenv.validate(example_values(**role, MONITORING_MODE=""))
            seen = bash_eval(nodeenv.shell_exports(env), ["MONITORING_MODE"])
            self.assertEqual(seen["MONITORING_MODE"], expected)

    def test_multiline_and_empty_values_survive(self):
        env = nodeenv.validate(
            example_values(
                MONITORING_OPERATOR_REMOTE_WRITE_URL="",
                MONITORING_NODE_NAME="two\nlines",
            )
        )
        seen = bash_eval(
            nodeenv.shell_exports(env),
            ["MONITORING_OPERATOR_REMOTE_WRITE_URL", "MONITORING_NODE_NAME"],
        )
        self.assertEqual(seen["MONITORING_OPERATOR_REMOTE_WRITE_URL"], "")
        self.assertEqual(seen["MONITORING_NODE_NAME"], "two\nlines")

    def test_cli_export_prints_the_block_and_nothing_else(self):
        with tempfile.TemporaryDirectory() as directory:
            path = Path(directory) / "node.env"
            values = example_values(RETH_DATA_DIR="/persistence/reth # fast disk")
            path.write_text("".join(f"{k}={v}\n" for k, v in values.items()))
            stdout = io.StringIO()
            with contextlib.redirect_stdout(stdout):
                code = nodeenv.main(["--export", "--no-ownership", str(path)])
            self.assertEqual(code, 0)
            seen = bash_eval(stdout.getvalue(), ["RETH_DATA_DIR"])
            self.assertEqual(seen["RETH_DATA_DIR"], "/persistence/reth # fast disk")

    def test_cli_export_prints_nothing_for_an_invalid_file(self):
        with tempfile.TemporaryDirectory() as directory:
            path = Path(directory) / "node.env"
            path.write_text("SEISMIC_ROLE=validator\nBOGUS=1\n")
            stdout, stderr = io.StringIO(), io.StringIO()
            with (
                contextlib.redirect_stdout(stdout),
                contextlib.redirect_stderr(stderr),
            ):
                code = nodeenv.main(["--export", "--no-ownership", str(path)])
            self.assertEqual(code, 1)
            self.assertEqual(stdout.getvalue(), "")
            self.assertIn("unknown keys", stderr.getvalue())


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

"""Exercise step_binaries with prebuilt binaries, in particular the operator-
supplied mdbx_copy a prebuilt Reth needs when the checkpointer is enabled.

Runs the real step_binaries/install_component dispatch with filesystem side
effects stubbed; no root-owned paths, downloads, or live services are used.
"""

from __future__ import annotations

import os
import subprocess
import sys
import tempfile
import unittest
from pathlib import Path

ROOT = Path(__file__).resolve().parents[1]
sys.path.insert(0, str(ROOT / "tools"))

from seismic_node import nodeenv

LIBMDBX = "v0.13.7-0-g566b0f93"
OTHER_LIBMDBX = "v0.13.6-0-ga971c76a"
# The example leaves network-specific values blank; fill them so it validates.
REQUIRED = {
    "CUSTODIAN_CHAIN_ID": "5124",
    "DOMAIN": "node.example.com",
    "MONITORING_NODE_NAME": "node-1",
    "MONITORING_REMOTE_WRITE_URL": "https://metrics.example/api/v1/write",
}
PREBUILT = {
    "SUMMIT": "summit",
    "RETH": "seismic-reth",
    "CUSTODIAN": "seismic-centralized-custodian-service",
    "CHECKPOINTER": "seismic-checkpointer",
}


class PrebuiltBinariesTests(unittest.TestCase):
    def setUp(self):
        temporary = tempfile.TemporaryDirectory(prefix="prebuilt-binaries-")
        self.addCleanup(temporary.cleanup)
        self.root = Path(temporary.name)
        self.values = nodeenv.parse(
            (ROOT / "install/templates/node.env.example").read_text()
        )
        self.values.update(REQUIRED)
        self.prebuilt = self.root / "prebuilt"
        self.prebuilt.mkdir()
        for key, binary in PREBUILT.items():
            self.values[f"{key}_INSTALL_METHOD"] = "prebuilt"
            self.values[f"{key}_PREBUILT_PATH"] = str(self.executable(binary))
        self.values["RETH_MDBX_COPY_PREBUILT_PATH"] = str(
            self.executable("mdbx_copy", [LIBMDBX])
        )

    def executable(self, name, embedded=()):
        """A fake binary: libmdbx's git-describe is a NUL-terminated C string
        in the real ones, so a line of its own stands in for it here."""
        path = self.prebuilt / name
        path.write_text(
            "#!/bin/sh\nexit 0\n"
            + "".join(f"\0{text}\0\n" for text in embedded)
            + "v1.2.3 trailing text that is not a libmdbx tag\n"
        )
        path.chmod(0o755)
        return path

    def run_step(self, security_stub=True, **overrides):
        values = {**self.values, **overrides}
        nodeenv.validate(dict(values))
        run = self.root / "run"
        if run.exists():
            for child in run.iterdir():
                child.unlink()
        run.mkdir(exist_ok=True)
        environment = dict(os.environ)
        environment.update(values)
        environment["PREBUILT_TEST_ROOT"] = str(run)
        stubs = "check_service_executable_security() { :; }\n" if security_stub else ""
        result = subprocess.run(
            [
                "bash",
                "-c",
                """set -euo pipefail
source "$1/install/lib/common.sh"
source "$1/install/lib/binaries.sh"
source "$1/install/lib/steps.sh"
LOG_FILE="$2/install.log"
download_component() { die "unexpected download: $*"; }
build_component() { die "unexpected source build: $*"; }
install_binary_target() {
    [[ "${3%/*}" == /usr/local/bin ]] || die "unexpected target $3"
    printf '%s %s\\n' "${3##*/}" "$2" >> "$PREBUILT_TEST_ROOT/installed"
}
"""
                + stubs
                + "step_binaries\n",
                "fixture",
                str(ROOT),
                str(run),
            ],
            env=environment,
            capture_output=True,
            text=True,
            check=False,
            timeout=30,
        )
        installed = run / "installed"
        return (
            result,
            [line.split(" ", 1) for line in installed.read_text().splitlines()]
            if installed.exists()
            else [],
        )

    def assert_installed(self, installed, *names):
        self.assertEqual([name for name, _ in installed], list(names))

    def test_prebuilt_reth_without_checkpointer_installs_reth_alone(self):
        self.executable("seismic-reth", [LIBMDBX])
        result, installed = self.run_step(CHECKPOINTER_ENABLED="false")
        self.assertEqual(result.returncode, 0, result.stdout + result.stderr)
        self.assert_installed(
            installed, "summit", "seismic-reth", PREBUILT["CUSTODIAN"]
        )
        self.assertNotIn("mdbx_copy", result.stdout + result.stderr)

    def test_prebuilt_reth_with_checkpointer_installs_matching_mdbx_copy(self):
        self.executable("seismic-reth", [LIBMDBX])
        result, installed = self.run_step(CHECKPOINTER_ENABLED="true")
        self.assertEqual(result.returncode, 0, result.stdout + result.stderr)
        self.assert_installed(
            installed,
            "summit",
            "seismic-reth",
            "mdbx_copy",
            PREBUILT["CHECKPOINTER"],
            PREBUILT["CUSTODIAN"],
        )
        self.assertEqual(
            dict(installed)["mdbx_copy"], self.values["RETH_MDBX_COPY_PREBUILT_PATH"]
        )
        output = result.stdout + result.stderr
        self.assertIn(
            "must be built from the same seismic-reth checkout (vendored libmdbx)",
            output,
        )
        self.assertIn(f"both link libmdbx {LIBMDBX}", output)

    def test_mismatched_mdbx_copy_aborts_before_reth_is_installed(self):
        self.executable("seismic-reth", [OTHER_LIBMDBX])
        result, installed = self.run_step(CHECKPOINTER_ENABLED="true")
        self.assertNotEqual(result.returncode, 0)
        self.assertIn(f"is built from libmdbx {LIBMDBX} but", result.stderr)
        self.assertIn(f"links libmdbx {OTHER_LIBMDBX}", result.stderr)
        self.assertIn(
            "make -C crates/storage/libmdbx-rs/mdbx-sys/libmdbx mdbx_copy",
            result.stderr,
        )
        self.assert_installed(installed, "summit")

    def test_binaries_without_a_single_libmdbx_version_are_rejected(self):
        cases = {
            "reth without libmdbx": ("seismic-reth", [], "no libmdbx version found in"),
            "mdbx_copy without libmdbx": (
                "mdbx_copy",
                [],
                "is it libmdbx's mdbx_copy?",
            ),
            "reth with two": (
                "seismic-reth",
                [LIBMDBX, OTHER_LIBMDBX],
                f"carries several libmdbx versions ({OTHER_LIBMDBX}, {LIBMDBX})",
            ),
            "mdbx_copy with two": (
                "mdbx_copy",
                [LIBMDBX, OTHER_LIBMDBX],
                "carries several libmdbx versions",
            ),
        }
        for name, (binary, embedded, message) in cases.items():
            with self.subTest(case=name):
                self.executable("seismic-reth", [LIBMDBX])
                self.executable("mdbx_copy", [LIBMDBX])
                self.executable(binary, embedded)
                result, installed = self.run_step(CHECKPOINTER_ENABLED="true")
                self.assertNotEqual(result.returncode, 0)
                self.assertIn(message, result.stderr)
                self.assert_installed(installed, "summit")

    def test_unsafe_prebuilt_files_are_reported_with_the_reason(self):
        # The fixtures are not root-owned, so the real check rejects them.
        self.executable("seismic-reth", [LIBMDBX])
        result, installed = self.run_step(security_stub=False)
        self.assertNotEqual(result.returncode, 0)
        self.assertIn(
            f"Summit: {self.values['SUMMIT_PREBUILT_PATH']} is not safe to install "
            "(file is not owned by root)",
            result.stderr,
        )
        self.assertEqual(installed, [])
        result, installed = self.run_step(
            security_stub=False,
            CHECKPOINTER_ENABLED="true",
            SUMMIT_INSTALL_METHOD="download",
        )
        self.assertIn("unexpected download: Summit", result.stderr)

    def test_libmdbx_describe_matches_real_release_strings(self):
        """The extraction must see libmdbx's string even when it is wedged
        between other data, and must not pick up look-alikes."""
        path = self.prebuilt / "blob"
        path.write_bytes(
            b"\x00\x01garbage\x00" + LIBMDBX.encode() + b"\x00more\x00"
            b"not-a-tag v0.13.7-0-g566b0f93 inside a sentence\x00"
            b"v1.2.3\x00v1.2.3-4-gzzzz\x00"
        )
        result = subprocess.run(
            [
                "bash",
                "-c",
                (
                    f'set -euo pipefail; source "{ROOT}/install/lib/binaries.sh"; '
                    f'libmdbx_describe "{path}"; echo "rc=$?"'
                ),
            ],
            capture_output=True,
            text=True,
            check=False,
            timeout=10,
        )
        self.assertEqual(result.stdout, f"{LIBMDBX}\nrc=0\n", result.stderr)


if __name__ == "__main__":
    unittest.main()

"""Exercise the real release-download flow with local assets and a fake gh.

No network, root-owned paths, downloaded executables or live services are used.
Tar extraction and checksum validation remain real; cryptographic attestation
verification is deliberately stubbed to test repository policy and ordering.
"""

from __future__ import annotations

import hashlib
import io
import json
import os
import subprocess
import sys
import tarfile
import tempfile
import unittest
from pathlib import Path

ROOT = Path(__file__).resolve().parents[1]
sys.path.insert(0, str(ROOT / "tools"))

from seismic_node import nodeenv

COMPONENTS = (
    ("SUMMIT", "summit", ("summit",)),
    ("RETH", "seismic-reth", ("seismic-reth", "mdbx_copy")),
    ("CHECKPOINTER", "seismic-checkpointer", ("seismic-checkpointer",)),
    ("CUSTODIAN", "enclave", ("seismic-centralized-custodian-service",)),
)


class ReleaseDownloadTests(unittest.TestCase):
    def setUp(self):
        temporary = tempfile.TemporaryDirectory(prefix="release-download-")
        self.addCleanup(temporary.cleanup)
        self.root = Path(temporary.name)
        self.values = nodeenv.parse(
            (ROOT / "install/templates/node.env.example").read_text()
        )
        self.releases = self.root / "releases"
        self.run_count = 0
        for key, repo, binaries in COMPONENTS:
            version = self.values[f"{key}_VERSION"]
            directory = self.releases / repo / version
            directory.mkdir(parents=True)
            asset = f"{binaries[0]}_{version}_linux_amd64"
            bodies = {
                binary: json.dumps(
                    {"repo": f"SeismicSystems/{repo}", "binary": binary}
                ).encode()
                for binary in binaries
            }
            archive = directory / f"{asset}.tar.gz"
            with tarfile.open(archive, "w:gz") as bundle:
                for binary, body in bodies.items():
                    info = tarfile.TarInfo(binary)
                    info.size = len(body)
                    info.mode = 0o755
                    bundle.addfile(info, io.BytesIO(body))
            attestation = directory / f"{asset}.sigstore.json"
            attestation.write_text(json.dumps({"repo": f"SeismicSystems/{repo}"}))
            checksums = {
                **bodies,
                archive.name: archive.read_bytes(),
                attestation.name: attestation.read_bytes(),
            }
            (directory / "SHA256SUMS").write_text(
                "".join(
                    f"{hashlib.sha256(body).hexdigest()}  {name}\n"
                    for name, body in checksums.items()
                )
            )
        self.binary_dir = self.root / "bin"
        self.binary_dir.mkdir()
        stub = self.binary_dir / "fixture"
        stub.write_text(
            f"#!{sys.executable}\n"
            + """import json, os, shutil, sys
from pathlib import Path
from urllib.parse import urlparse
name = Path(sys.argv[0]).name
args = sys.argv[1:]
root = Path(os.environ["RELEASE_TEST_ROOT"])
def record(event):
    with (root / "events.jsonl").open("a") as log:
        log.write(json.dumps(event) + "\\n")
if name == "curl":
    parts = urlparse(args[-1]).path.split("/")
    assert parts[1] == "SeismicSystems" and parts[3:5] == ["releases", "download"]
    source = Path(os.environ["RELEASE_TEST_ASSETS"]) / parts[2] / parts[5] / parts[6]
    shutil.copyfile(source, args[args.index("-o") + 1])
elif name == "gh":
    record({"command": "verify", "args": args})
    assert args[:2] == ["attestation", "verify"]
    artifact = Path(args[2])
    expected = json.loads(artifact.read_text())["repo"]
    if "--repo" not in args or args[args.index("--repo") + 1] != expected or "--owner" in args:
        sys.exit("attestation repository policy mismatch")
    bundle = Path(args[args.index("--bundle") + 1])
    assert json.loads(bundle.read_text())["repo"] == expected
    if artifact.name == os.environ.get("RELEASE_TEST_FAIL_BINARY"):
        sys.exit("fixture attestation verification failed")
elif name == "fixture-install":
    source, destination = Path(args[1]), Path(args[2])
    assert destination.parent == Path("/usr/local/bin")
    record({"command": "install", "binary": source.name})
    installed = root / "installed"
    installed.mkdir(exist_ok=True)
    shutil.copyfile(source, installed / destination.name)
else:
    raise AssertionError(name)
"""
        )
        stub.chmod(0o755)
        for name in ("curl", "gh", "fixture-install"):
            (self.binary_dir / name).symlink_to(stub)

    def download(self, fail_binary=""):
        self.run_count += 1
        run = self.root / f"run-{self.run_count}"
        run.mkdir()
        environment = dict(os.environ)
        environment.update(self.values)
        environment.update(
            PATH=f"{self.binary_dir}{os.pathsep}{os.environ['PATH']}",
            CHECKPOINTER_ENABLED="true",
            RELEASE_TEST_ROOT=str(run),
            RELEASE_TEST_ASSETS=str(self.releases),
            RELEASE_TEST_FAIL_BINARY=fail_binary,
        )
        result = subprocess.run(
            [
                "bash",
                "-c",
                """set -euo pipefail
source "$1/install/lib/steps.sh"
DOWNLOAD_DIR="$2/downloads"
LOG_FILE="$2/install.log"
info() { printf '%s\\n' "$*"; }
success() { printf '%s\\n' "$*"; }
die() { printf '%s\\n' "$*" >&2; exit 1; }
ensure_directory() { mkdir -p -- "$1"; }
install() {
    [[ "$1" == -d ]] || die "unexpected install operation"
    mkdir -p -- "${@: -1}"
}
install_binary_target() { fixture-install "$@"; }
step_binaries
""",
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
        events = run / "events.jsonl"
        return (
            result,
            run,
            [json.loads(line) for line in events.read_text().splitlines()]
            if events.exists()
            else [],
        )

    def test_each_component_and_mdbx_copy_require_their_own_repository(self):
        result, run, events = self.download()
        self.assertEqual(
            result.returncode,
            0,
            result.stdout + result.stderr + (run / "install.log").read_text(),
        )
        expected = []
        for key, repo, binaries in COMPONENTS:
            version = self.values[f"{key}_VERSION"]
            directory = run / "downloads" / f"{repo}-{version}"
            asset = f"{binaries[0]}_{version}_linux_amd64"
            for binary in binaries:
                expected.extend(
                    [
                        {
                            "command": "verify",
                            "args": [
                                "attestation",
                                "verify",
                                str(directory / binary),
                                "--repo",
                                f"SeismicSystems/{repo}",
                                "--bundle",
                                str(directory / f"{asset}.sigstore.json"),
                            ],
                        },
                        {"command": "install", "binary": binary},
                    ]
                )
                self.assertEqual(
                    json.loads((run / "installed" / binary).read_text())["repo"],
                    f"SeismicSystems/{repo}",
                )
        self.assertEqual(events, expected)
        self.assertEqual(list((run / "downloads").iterdir()), [])

    def test_attestation_failure_never_installs_the_failed_or_later_binaries(self):
        binaries = [binary for _, _, group in COMPONENTS for binary in group]
        for index, binary in enumerate(binaries):
            with self.subTest(binary=binary):
                result, run, events = self.download(fail_binary=binary)
                self.assertNotEqual(result.returncode, 0, result.stdout + result.stderr)
                self.assertIn(
                    f"build-provenance attestation failed for {binary}", result.stderr
                )
                self.assertIn(
                    "fixture attestation verification failed",
                    (run / "install.log").read_text(),
                )
                self.assertEqual(
                    [e["binary"] for e in events if e["command"] == "install"],
                    binaries[:index],
                )
                self.assertEqual(
                    [
                        Path(e["args"][2]).name
                        for e in events
                        if e["command"] == "verify"
                    ],
                    binaries[: index + 1],
                )
                self.assertFalse((run / "installed" / binary).exists())

    def test_runbook_fetch_refuses_failed_verification_without_errexit(self):
        text = (ROOT / "docs/RUNBOOK.md").read_text()
        function = (
            "fetch() {" + text.split("fetch() {", 1)[1].split("\n}\n", 1)[0] + "\n}\n"
        )
        result = subprocess.run(
            [
                "bash",
                "-c",
                """set -u
curl() { :; }
sha256sum() { :; }
tar() { :; }
gh() { printf '%s\\n' "$*"; return 42; }
sudo() { printf 'UNEXPECTED INSTALL\\n'; exit 99; }
"""
                + function
                + "fetch summit v0.1.0 summit\n",
            ],
            cwd=self.root,
            capture_output=True,
            text=True,
            check=False,
            timeout=10,
        )
        self.assertEqual(result.returncode, 1, result.stdout + result.stderr)
        self.assertIn("--repo SeismicSystems/summit", result.stdout)
        self.assertNotIn("--owner", result.stdout)
        self.assertNotIn("UNEXPECTED INSTALL", result.stdout)

    def test_checksum_failure_prevents_attestation_and_installation(self):
        directory = self.releases / "summit" / self.values["SUMMIT_VERSION"]
        archive = next(directory.glob("*.tar.gz"))
        archive.write_bytes(archive.read_bytes() + b"tampered")
        result, run, events = self.download()
        self.assertNotEqual(result.returncode, 0, result.stdout + result.stderr)
        self.assertIn("SHA256SUMS mismatch", result.stderr)
        self.assertEqual(events, [])
        self.assertFalse((run / "installed").exists())


if __name__ == "__main__":
    unittest.main()

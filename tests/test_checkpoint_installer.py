"""Focused safety tests for the checkpoint installer."""

from __future__ import annotations

import importlib.util
import io
import json
import tarfile
import tempfile
import unittest
from collections.abc import Callable
from pathlib import Path
from unittest import mock

REPOSITORY_ROOT = Path(__file__).resolve().parents[1]
INSTALLER_PATH = REPOSITORY_ROOT / "tools/checkpoint-start/install-checkpoint.py"
SPEC = importlib.util.spec_from_file_location("checkpoint_installer", INSTALLER_PATH)
assert SPEC is not None and SPEC.loader is not None
installer = importlib.util.module_from_spec(SPEC)
SPEC.loader.exec_module(installer)


class CheckpointInstallerTests(unittest.TestCase):
    def assert_checkpoint_error(
        self, expected: str, callback: Callable[[], object]
    ) -> None:
        with self.assertRaisesRegex(installer.CheckpointError, expected):
            callback()

    def validate_tar_members(self, members: list[tarfile.TarInfo]) -> None:
        archive_bytes = io.BytesIO()
        with tarfile.open(fileobj=archive_bytes, mode="w") as output:
            for member in members:
                payload = io.BytesIO(b"x") if member.isfile() else None
                output.addfile(member, payload)
        archive_bytes.seek(0)
        with tarfile.open(fileobj=archive_bytes, mode="r") as archive:
            installer.validate_tar_members(archive)

    def test_archive_rejects_traversal_links_and_duplicates(self) -> None:
        traversal = tarfile.TarInfo("../escape")
        traversal.size = 1
        self.assert_checkpoint_error(
            "Unsafe snapshot archive path",
            lambda: self.validate_tar_members([traversal]),
        )

        link = tarfile.TarInfo("db/link")
        link.type = tarfile.SYMTYPE
        link.linkname = "/etc/passwd"
        self.assert_checkpoint_error(
            "Unsupported snapshot archive member type",
            lambda: self.validate_tar_members([link]),
        )

        duplicate_a = tarfile.TarInfo("metadata.json")
        duplicate_a.size = 1
        duplicate_b = tarfile.TarInfo("metadata.json")
        duplicate_b.size = 1
        self.assert_checkpoint_error(
            "Duplicate snapshot archive path",
            lambda: self.validate_tar_members([duplicate_a, duplicate_b]),
        )

    def test_weak_subjectivity_anchor_must_be_recent(self) -> None:
        value = {"epoch": 1, "header_digest": "0x" + "11" * 32}
        with mock.patch.object(installer, "read_toml", return_value=value):
            self.assert_checkpoint_error(
                "more than 5 epochs",
                lambda: installer.load_weak_subjectivity(Path("/unused"), 7),
            )

    def test_replacement_paths_must_not_be_nested(self) -> None:
        self.assert_checkpoint_error(
            "must not be identical or nested",
            lambda: installer.require_separate_paths(
                {"Reth": Path("/state"), "Summit": Path("/state/summit")}
            ),
        )

    def test_finalized_header_history_must_be_contiguous(self) -> None:
        with tempfile.TemporaryDirectory() as temporary_directory:
            stage = Path(temporary_directory)
            (stage / "db").mkdir()
            (stage / "db/mdbx.dat").write_bytes(b"db")
            (stage / "static_files").mkdir()
            checkpoint = stage / "summit_checkpoint"
            headers = checkpoint / "finalized_headers"
            headers.mkdir(parents=True)
            (checkpoint / "checkpoint").write_bytes(b"checkpoint")
            (checkpoint / "last_block").write_bytes(b"block")
            (checkpoint / "finalized_header").write_bytes(b"header-2")
            (headers / "0").write_bytes(b"header-0")
            (headers / "2").write_bytes(b"header-2")
            (stage / "metadata.json").write_text(
                json.dumps(
                    {
                        "epoch": 2,
                        "block_number": 10,
                        "timestamp": "2026-01-01T00:00:00Z",
                    }
                )
            )
            manifest = {"epoch": 2, "execution": {"block_number": 10}}
            self.assert_checkpoint_error(
                "exactly epochs 0 through 2",
                lambda: installer.validate_extracted_snapshot(stage, manifest),
            )

    def test_partial_rollback_does_not_delete_unmoved_state(self) -> None:
        with tempfile.TemporaryDirectory() as temporary_directory:
            root = Path(temporary_directory)
            reth = root / "reth"
            summit = root / "summit"
            checkpoint = root / "checkpoint"
            config = root / "config.toml"
            weak_subjectivity = root / "weak.toml"
            backup = root / "backup"

            (reth / "db").mkdir(parents=True)
            (reth / "db/new").write_bytes(b"new-db")
            (reth / "static_files").mkdir()
            (reth / "static_files/original").write_bytes(b"original-static")
            summit.mkdir()
            (summit / installer.LOCK_FILE_NAME).touch()
            (summit / "not-backed-up").write_bytes(b"leave-me")
            checkpoint.mkdir()
            (checkpoint / "original").write_bytes(b"leave-checkpoint")
            config.write_bytes(b"leave-config")
            weak_subjectivity.write_bytes(b"leave-anchor")

            (backup / "reth/db").mkdir(parents=True)
            (backup / "reth/db/original").write_bytes(b"original-db")
            (backup / "summit-data").mkdir(parents=True)
            (backup / "summit-data/backed-up").write_bytes(b"old-summit")

            receipt = {
                "paths": {
                    "reth_data_dir": str(reth),
                    "summit_data_dir": str(summit),
                    "checkpoint_path": str(checkpoint),
                    "checkpoint_config_path": str(config),
                    "weak_subjectivity_path": str(weak_subjectivity),
                },
                "previous": {
                    "reth_db": True,
                    "reth_static_files": True,
                    "checkpoint": True,
                    "checkpoint_config": True,
                    "weak_subjectivity": True,
                },
            }
            installer.restore_backup(
                backup,
                receipt,
                remove_new_summit_state=False,
            )

            self.assertEqual((reth / "db/original").read_bytes(), b"original-db")
            self.assertEqual(
                (reth / "static_files/original").read_bytes(), b"original-static"
            )
            self.assertEqual((summit / "not-backed-up").read_bytes(), b"leave-me")
            self.assertEqual((summit / "backed-up").read_bytes(), b"old-summit")
            self.assertTrue((checkpoint / "original").exists())
            self.assertEqual(config.read_bytes(), b"leave-config")
            self.assertEqual(weak_subjectivity.read_bytes(), b"leave-anchor")


if __name__ == "__main__":
    unittest.main()

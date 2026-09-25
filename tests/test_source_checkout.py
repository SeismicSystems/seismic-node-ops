"""Exercise installer Git refs using local repositories, never live hosts."""

import os
import subprocess
import tempfile
import unittest
from pathlib import Path

ROOT = Path(__file__).resolve().parents[1]
TAG = "internal-testnet-v0"


class SourceCheckoutTests(unittest.TestCase):
    def setUp(self):
        temporary = tempfile.TemporaryDirectory()
        self.addCleanup(temporary.cleanup)
        self.root = Path(temporary.name)
        self.env = {k: v for k, v in os.environ.items() if not k.startswith("GIT_")}
        self.env.update(
            GIT_CONFIG_NOSYSTEM="1",
            GIT_CONFIG_GLOBAL=os.devnull,
            GIT_TERMINAL_PROMPT="0",
            GIT_AUTHOR_NAME="Fixture",
            GIT_AUTHOR_EMAIL="fixture@example.invalid",
            GIT_COMMITTER_NAME="Fixture",
            GIT_COMMITTER_EMAIL="fixture@example.invalid",
        )
        self.remote = self.root / "remote.git"
        self.seed = self.root / "seed"
        self.checkout = self.root / "service/src/component"
        self.log = self.root / "installer.log"
        self.git("init", "--bare", "--initial-branch=main", str(self.remote))
        self.git("init", "--initial-branch=main", str(self.seed))
        self.first = self.commit("initial")
        self.git("branch", "other", cwd=self.seed)
        self.git("remote", "add", "origin", self.remote.as_uri(), cwd=self.seed)
        self.git("push", "origin", "main", "other", cwd=self.seed)
        self.publish_tag(TAG)

    def git(self, *args, cwd=None, check=True):
        return subprocess.run(
            ["git", *args],
            cwd=cwd or self.root,
            env=self.env,
            capture_output=True,
            text=True,
            check=check,
            timeout=20,
        )

    def commit(self, text):
        (self.seed / "payload").write_text(text + "\n")
        self.git("add", "payload", cwd=self.seed)
        self.git("commit", "-m", text, cwd=self.seed)
        return self.git("rev-parse", "HEAD", cwd=self.seed).stdout.strip()

    def publish_tag(self, name, annotated=False):
        args = ["tag"]
        if annotated:
            args.extend(["-a", "-m", "fixture release"])
        self.git(*args, name, cwd=self.seed)
        self.git("push", "origin", f"refs/tags/{name}", cwd=self.seed)

    def prepare(self, ref=f"refs/tags/{TAG}", expected=True):
        result = subprocess.run(
            [
                "bash",
                "-c",
                """set -euo pipefail
source "$1/install/lib/binaries.sh"
SERVICE_HOME="$2/service"
LOG_FILE="$2/installer.log"
run_as_service_user() { "$@"; }
prepare_source_root() { mkdir -p "$SERVICE_HOME/src"; }
info() { printf '%s\\n' "$*"; }
success() { printf '%s\\n' "$*"; }
die() { printf '%s\\n' "$*" >&2; exit 1; }
prepare_source_checkout "Fixture" "$3" "$4" "$5"
""",
                "fixture",
                str(ROOT),
                str(self.root),
                self.remote.as_uri(),
                str(self.checkout),
                ref,
            ],
            env=self.env,
            capture_output=True,
            text=True,
            check=False,
            timeout=30,
        )
        diagnostic = result.stdout + result.stderr
        if self.log.exists():
            diagnostic += self.log.read_text()
        if expected:
            self.assertEqual(result.returncode, 0, diagnostic)
        else:
            self.assertNotEqual(result.returncode, 0, diagnostic)
        return result

    def head(self):
        return self.git("rev-parse", "HEAD", cwd=self.checkout).stdout.strip()

    def assert_detached(self, commit):
        self.assertEqual(self.head(), commit)
        self.assertEqual(
            self.git(
                "symbolic-ref", "-q", "HEAD", cwd=self.checkout, check=False
            ).returncode,
            1,
        )
        self.assertEqual(
            self.git("status", "--porcelain", cwd=self.checkout).stdout, ""
        )

    def test_fresh_lightweight_tag_and_rerun_stay_pinned(self):
        self.prepare()
        self.assert_detached(self.first)
        newer = self.commit("branch advanced")
        self.git("push", "origin", "main", cwd=self.seed)
        self.prepare()
        self.assert_detached(self.first)
        self.assertNotEqual(self.head(), newer)
        self.git("show-ref", "--verify", "refs/remotes/origin/other", cwd=self.checkout)

    def test_annotated_tag_resolves_commit_and_survives_rerun(self):
        self.publish_tag("annotated-release", annotated=True)
        self.prepare("refs/tags/annotated-release")
        self.assert_detached(self.first)
        self.assertNotEqual(
            self.git(
                "rev-parse", "refs/tags/annotated-release", cwd=self.checkout
            ).stdout.strip(),
            self.first,
        )
        self.prepare("refs/tags/annotated-release")
        self.assert_detached(self.first)

    def test_legacy_single_branch_clone_can_migrate_to_tag(self):
        self.checkout.parent.mkdir(parents=True)
        self.git(
            "clone",
            "--single-branch",
            "--no-tags",
            "--branch",
            "other",
            self.remote.as_uri(),
            str(self.checkout),
        )
        newer = self.commit("new release")
        self.publish_tag("next-release", annotated=True)
        self.prepare("refs/tags/next-release")
        self.assert_detached(newer)
        self.assertEqual(
            self.git("rev-parse", "refs/heads/other", cwd=self.checkout).stdout.strip(),
            self.first,
        )

    def test_same_named_branch_does_not_override_explicit_tag(self):
        newer = self.commit("branch sharing the tag name")
        self.git("branch", TAG, newer, cwd=self.seed)
        self.git("push", "origin", f"refs/heads/{TAG}", cwd=self.seed)
        self.prepare()
        self.assert_detached(self.first)
        self.prepare()
        self.assert_detached(self.first)

    def test_moved_tag_refused_without_changing_head_or_local_tag(self):
        self.prepare()
        old_tag = self.git("rev-parse", f"refs/tags/{TAG}", cwd=self.checkout).stdout
        self.commit("retagged release")
        self.git("tag", "-f", TAG, cwd=self.seed)
        self.git("push", "--force", "origin", f"refs/tags/{TAG}", cwd=self.seed)
        result = self.prepare(expected=False)
        self.assertIn("missing or moved", result.stderr)
        self.assert_detached(self.first)
        self.assertEqual(
            self.git("rev-parse", f"refs/tags/{TAG}", cwd=self.checkout).stdout, old_tag
        )

    def test_replaced_annotated_tag_is_refused_even_at_same_commit(self):
        self.publish_tag("annotated-release", annotated=True)
        self.prepare("refs/tags/annotated-release")
        self.git(
            "tag",
            "-f",
            "-a",
            "-m",
            "changed annotation",
            "annotated-release",
            cwd=self.seed,
        )
        self.git(
            "push", "--force", "origin", "refs/tags/annotated-release", cwd=self.seed
        )
        self.prepare("refs/tags/annotated-release", expected=False)
        self.assert_detached(self.first)

    def test_deleted_tag_is_not_reused_or_replaced_by_same_named_branch(self):
        self.prepare()
        self.git("branch", TAG, cwd=self.seed)
        self.git(
            "push", "origin", f"refs/heads/{TAG}", f":refs/tags/{TAG}", cwd=self.seed
        )
        # Even user-configured pruning must not remove the local release pin.
        self.git("config", "fetch.prune", "true", cwd=self.checkout)
        self.git("config", "fetch.pruneTags", "true", cwd=self.checkout)
        self.prepare(expected=False)
        self.assert_detached(self.first)
        self.git("show-ref", "--verify", f"refs/tags/{TAG}", cwd=self.checkout)

    def test_missing_tag_does_not_fall_back_to_branch(self):
        self.git("branch", "branch-only", cwd=self.seed)
        self.git("push", "origin", "branch-only", cwd=self.seed)
        result = self.prepare("refs/tags/branch-only", expected=False)
        self.assertIn("missing or moved", result.stderr)

    def test_non_commit_tag_is_rejected(self):
        blob = self.git("rev-parse", "HEAD:payload", cwd=self.seed).stdout.strip()
        self.git("tag", "blob-tag", blob, cwd=self.seed)
        self.git("push", "origin", "refs/tags/blob-tag", cwd=self.seed)
        result = self.prepare("refs/tags/blob-tag", expected=False)
        self.assertIn("does not resolve to a commit", result.stderr)

    def test_dirty_tracked_and_untracked_files_are_preserved(self):
        self.prepare()
        for name in ("payload", "untracked"):
            with self.subTest(name=name):
                path = self.checkout / name
                original = path.read_bytes() if path.exists() else None
                path.write_text("operator changes")
                result = self.prepare(expected=False)
                self.assertIn("local changes", result.stderr)
                self.assertEqual(self.head(), self.first)
                self.assertEqual(path.read_text(), "operator changes")
                if original is None:
                    path.unlink()
                else:
                    path.write_bytes(original)

    def test_new_tag_can_be_selected_explicitly(self):
        self.prepare()
        newer = self.commit("next release")
        self.publish_tag("internal-testnet-v1")
        self.prepare("refs/tags/internal-testnet-v1")
        self.assert_detached(newer)
        self.assertEqual(
            self.git("rev-parse", f"refs/tags/{TAG}", cwd=self.checkout).stdout.strip(),
            self.first,
        )

    def test_origin_mismatch_refused(self):
        self.prepare()
        self.git(
            "remote",
            "set-url",
            "origin",
            (self.root / "wrong.git").as_uri(),
            cwd=self.checkout,
        )
        result = self.prepare(expected=False)
        self.assertIn("unexpected origin", result.stderr)
        self.assert_detached(self.first)

    def test_dangling_source_symlink_refused(self):
        self.checkout.parent.mkdir(parents=True)
        target = self.root / "must-not-create"
        self.checkout.symlink_to(target)
        result = self.prepare(expected=False)
        self.assertIn("symbolic link", result.stderr)
        self.assertFalse(target.exists())

    def test_branch_selector_does_not_accept_a_tag(self):
        self.publish_tag("tag-only")
        result = self.prepare("tag-only", expected=False)
        self.assertIn("use refs/tags/<tag>", result.stderr)

    def test_branch_clone_and_fast_forward_still_work(self):
        self.prepare("main")
        self.assertEqual(
            self.git("branch", "--show-current", cwd=self.checkout).stdout.strip(),
            "main",
        )
        self.git("show-ref", "--verify", "refs/remotes/origin/other", cwd=self.checkout)
        newer = self.commit("fast forward")
        self.git("push", "origin", "main", cwd=self.seed)
        self.prepare("refs/heads/main")
        self.assertEqual(self.head(), newer)

    def test_legacy_single_branch_clone_can_change_branch_and_return_from_tag(self):
        self.checkout.parent.mkdir(parents=True)
        self.git(
            "clone",
            "--single-branch",
            "--branch",
            "other",
            self.remote.as_uri(),
            str(self.checkout),
        )
        self.prepare("main")
        self.assertEqual(
            self.git(
                "config", "--get", "remote.origin.fetch", cwd=self.checkout
            ).stdout.strip(),
            "+refs/heads/*:refs/remotes/origin/*",
        )
        self.prepare()
        self.assert_detached(self.first)
        self.prepare("main")
        self.assertEqual(
            self.git("branch", "--show-current", cwd=self.checkout).stdout.strip(),
            "main",
        )

    def test_non_fast_forward_branch_update_refused(self):
        self.prepare("main")
        (self.checkout / "local").write_text("local commit")
        self.git("add", "local", cwd=self.checkout)
        self.git("commit", "-m", "local commit", cwd=self.checkout)
        local = self.head()
        self.commit("remote commit")
        self.git("push", "origin", "main", cwd=self.seed)
        result = self.prepare("main", expected=False)
        self.assertIn("fast-forward only", result.stderr)
        self.assertEqual(self.head(), local)

    def test_force_pushed_branch_history_refused(self):
        self.prepare("main")
        self.git("checkout", "--orphan", "replacement", cwd=self.seed)
        self.commit("replacement history")
        self.git("push", "--force", "origin", "HEAD:refs/heads/main", cwd=self.seed)
        self.prepare("main", expected=False)
        self.assertEqual(self.head(), self.first)

    def test_invalid_refs_refused_before_creating_checkout(self):
        for ref in (
            "",
            "--option",
            "HEAD",
            "refs/tags/",
            "refs/tags/v0^{commit}",
            "refs/remotes/origin/main",
            "refs/tags/a:b",
        ):
            with self.subTest(ref=ref):
                self.prepare(ref, expected=False)
                self.assertFalse(self.checkout.exists())

    def test_configured_refs_include_temporary_custodian_branch(self):
        result = subprocess.run(
            [
                "bash",
                "-c",
                """set -euo pipefail
source "$1/install/lib/configuration.sh"
section() { :; }
_out() { :; }
configure_component_installation() { :; }
print_component_installation() { :; }
confirm() { return 1; }
configure_node_software
configure_checkpointer
configure_custodian
printf '%s\\n' "$SUMMIT_SOURCE_REF" "$RETH_SOURCE_REF" "$CUSTODIAN_SOURCE_REF" "$CHECKPOINTER_SOURCE_REF" "$CUSTODIAN_REQUIRED_SUMMIT_REF" "$CUSTODIAN_REQUIRED_RETH_REF"
""",
                "fixture",
                str(ROOT),
            ],
            env=self.env,
            capture_output=True,
            text=True,
            check=False,
            timeout=10,
        )
        self.assertEqual(result.returncode, 0, result.stderr)
        self.assertEqual(
            result.stdout.splitlines(),
            [
                "refs/tags/internal-testnet-v1",  # Summit source selection
                "refs/tags/internal-testnet-v1",  # Reth source selection
                "refs/heads/centralized-custodian",  # Until the HTTP release is tagged.
                "main",  # Checkpointer
                TAG,  # Custodian's Summit compatibility baseline is unchanged.
                TAG,  # Custodian's Reth compatibility baseline is unchanged.
            ],
        )


if __name__ == "__main__":
    unittest.main()

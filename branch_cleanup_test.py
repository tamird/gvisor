import datetime as dt
import os
from pathlib import Path
import subprocess
import tempfile
import unittest
from unittest import mock

import branch_cleanup as cleanup


class BranchCleanupTest(unittest.TestCase):
    def test_payload_rejects_mixed_product_and_validation_changes(self):
        with mock.patch.object(cleanup, "git", return_value=(
                b"\x1eaaa\n\n.github/workflows/build.yml\ntest/rbe/report.py\n"
                b"\x1ebbb\n\n.github/workflows/build.yml\npkg/new-product.go\n")):
            self.assertEqual(cleanup.payload_paths({"aaa", "bbb"}), {
                "aaa": [".github/workflows/build.yml", "test/rbe/report.py"]})

    def test_reflog_archive_and_compare_and_swap(self):
        # This fixture uses only two disposable bare repos, with transport
        # restricted to file. A changed remote tip exercises a real stale lease.
        with tempfile.TemporaryDirectory(prefix="gvisor-cleanup-fixture-") as tmp:
            src, dst = (Path(tmp) / name for name in ("src.git", "dst.git"))
            private_env = {"GIT_ALLOW_PROTOCOL": "file", "GIT_CONFIG_NOSYSTEM": "1",
                           "GIT_CONFIG_GLOBAL": os.devnull, "GIT_TERMINAL_PROMPT": "0"}
            with mock.patch.dict(os.environ, private_env):
                for repo in (src, dst):
                    subprocess.run(["git", "init", "--bare", "--quiet", str(repo)], check=True)
                with mock.patch.dict(os.environ, {"GIT_DIR": str(src)}):
                    run = cleanup.git
                    run("config", "user.name", "Fixture")
                    run("config", "user.email", "fixture@example.com")
                    run("config", "core.logAllRefUpdates", "true")
                    run("remote", "add", "origin", str(dst))
                    tree = run("mktree").decode().strip()
                    one, two = (run("commit-tree", tree, "-m", msg).decode().strip() for msg in ("one", "two"))
                    descendant = run("commit-tree", tree, "-p", one, "-m", "descendant").decode().strip()
                    cousin = run("commit-tree", tree, "-p", one, "-m", "cousin").decode().strip()
                    fork = {"qualification/first": descendant, "qualification/second": cousin, "master": two}
                    dropping = {"qualification/first", "qualification/second"}
                    witness = cleanup.evidence_keepers(fork, dropping, two, {one[:9]})
                    self.assertEqual(len(witness), 1)
                    self.assertEqual(cleanup.evidence_keepers(fork, dropping - witness, two, {one[:9]}), set())
                    self.assertEqual(cleanup.evidence_keepers(fork, dropping, descendant, {one[:9]}), set())
                    run("update-ref", "refs/heads/master", descendant)
                    with mock.patch.dict(os.environ, {"GIT_DIR": str(dst)}):
                        (dst / "FETCH_HEAD").write_text("previous operation\n")
                        self.assertEqual(cleanup.available({descendant}), set())
                        cleanup.fetch_missing(str(src), {descendant})
                        self.assertEqual(cleanup.available({descendant}), {descendant})
                        self.assertEqual((dst / "FETCH_HEAD").read_text(), "previous operation\n")
                        self.assertEqual(run("for-each-ref", "--format=%(refname)"), b"")
                        with mock.patch.object(cleanup, "git", wraps=run) as observed:
                            cleanup.fetch_missing(str(src), {descendant})
                            self.assertFalse(any(call.args[0] == "fetch" for call in observed.call_args_list))
                    name = "qualification/fixture"
                    full = cleanup.HEADS + name
                    run("update-ref", full, one)
                    run("update-ref", full, two, one)
                    run("push", "origin", full)
                    # The old local branch must survive any stale expected-OID update.
                    row = cleanup.Candidate("local", name, two, "retired")
                    archive = cleanup.archive([row])
                    self.assertEqual(cleanup.archive([row]), archive)
                    self.assertEqual(run("show", archive + ":refs.tsv").decode(), f"local\t{name}\t{two}\n")
                    self.assertIn(one.encode(), run("show", archive + ":reflogs.json"))
                    self.assertEqual(run("merge-base", "--is-ancestor", one, archive), b"")
                    self.assertEqual(run("merge-base", "--is-ancestor", two, archive), b"")
                    with self.assertRaises(subprocess.CalledProcessError):
                        run("update-ref", "--stdin", data=cleanup.lines([f"delete {full} {one}"]))
                    self.assertEqual(run("rev-parse", full).decode().strip(), two)
                    with self.assertRaises(subprocess.CalledProcessError):
                        run("push", "--atomic", f"--force-with-lease={full}:{one}", "origin", ":" + full)
                    self.assertEqual(cleanup.remote_refs("origin"), {name: two})
                    run("push", "--atomic", f"--force-with-lease={full}:{two}", "origin", ":" + full)
                    run("update-ref", "--stdin", data=cleanup.lines([f"delete {full} {two}"]))
                    self.assertEqual(cleanup.remote_refs("origin"), {})
                    self.assertEqual(run("for-each-ref", "--format=%(refname)", "refs/branch-cleanup").decode().strip(),
                                     "refs/branch-cleanup/archive")
                    run("update-ref", full, two, "0" * 40)
                    self.assertEqual(run("rev-parse", full).decode().strip(), two)
                    self.assertEqual(cleanup.local_activity({name: two}, dt.datetime.now(dt.timezone.utc)
                                                           - dt.timedelta(days=7)), {name})


if __name__ == "__main__":
    unittest.main()

import datetime as dt
import contextlib
import os
from pathlib import Path
import subprocess
import tempfile
import unittest
from unittest import mock

import branch_cleanup as cleanup


class BranchCleanupTest(unittest.TestCase):
    def test_completed_run_waits_after_latest_attempt_on_branch(self):
        now = dt.datetime.fromisoformat("2026-11-11T12:00:00+00:00")
        runs = [
            [1, "idle", "a", "2026-11-10T12:00:00Z", "push", cleanup.FORK],
            [2, "retried", "b", "2026-11-09T00:00:00Z", "push", cleanup.FORK],
            [3, "retried", "c", "2026-11-11T11:59:00Z", "workflow_dispatch", cleanup.FORK],
            [4, "other-fork", "d", "2026-11-09T00:00:00Z", "push", "another/gvisor"],
        ]
        with mock.patch.object(cleanup, "api", side_effect=lambda path, *args, **kwargs:
                               [[len(runs), runs]] if "created=2026-11-09" in path else [[0, []]]):
            self.assertEqual(cleanup.completed_runs(now), {("idle", "a")})
        with mock.patch.object(cleanup, "api", return_value=[[5, runs]]):
            with self.assertRaisesRegex(ValueError, "incomplete completed Actions inventory"):
                cleanup.completed_runs(now)

    def test_named_sources_in_tracker_urls(self):
        self.assertEqual(cleanup.linked_branches(
            "https://github.com/tamird/gvisor/blob/qualification/a/file.go "
            "https://github.com/tamird/gvisor/compare/base-name...tip/with-slash "
            "https://github.com/google/gvisor/compare/master...tamird:gvisor:other/tip"
        ), {"qualification", "qualification/a", "qualification/a/file.go", "base-name", "tip/with-slash", "other/tip"})

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
            old_date = (dt.datetime.now(dt.timezone.utc) - dt.timedelta(days=2)).isoformat()
            private_env = {"GIT_ALLOW_PROTOCOL": "file", "GIT_CONFIG_NOSYSTEM": "1",
                           "GIT_CONFIG_GLOBAL": os.devnull, "GIT_TERMINAL_PROMPT": "0",
                           "GIT_AUTHOR_DATE": old_date, "GIT_COMMITTER_DATE": old_date}
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
                    two = run("commit-tree", tree, "-m", "upstream").decode().strip()
                    blob = run("hash-object", "-w", "--stdin", data=b"content").decode().strip()
                    source_line = f"100644 blob {blob}\tproduct.go"
                    source_tree = run("mktree", data=cleanup.lines([source_line])).decode().strip()
                    one = run("commit-tree", source_tree, "-p", two, "-m", "unsubmitted fix").decode().strip()
                    rbe = run("mktree", data=cleanup.lines([f"100644 blob {blob}\tcheck"])).decode().strip()
                    tests = run("mktree", data=cleanup.lines([f"040000 tree {rbe}\trbe"])).decode().strip()
                    ci_tree = run("mktree", data=cleanup.lines([source_line, f"040000 tree {tests}\ttest"])).decode().strip()
                    descendant = run("commit-tree", ci_tree, "-p", one, "-m", "first run").decode().strip()
                    cousin = run("commit-tree", ci_tree, "-p", one, "-m", "second run").decode().strip()
                    fork = {"qualification/first": descendant, "qualification/second": cousin, "master": two}
                    dropping = {"qualification/first", "qualification/second"}
                    witness = cleanup.evidence_keepers(fork, dropping, two, {one[:9]})
                    self.assertEqual(len(witness), 1)
                    self.assertEqual(cleanup.evidence_keepers(fork, dropping - witness, two, {one[:9]}), set())
                    self.assertEqual(cleanup.evidence_keepers(fork, dropping, descendant, {one[:9]}), set())
                    self.assertEqual(cleanup.evidence_keepers(fork, dropping, two, set()), witness)
                    self.assertEqual(cleanup.evidence_keepers(fork, dropping, one, set()), set())
                    self.assertEqual(cleanup.evidence_keepers(fork, dropping, one, {cousin[:9]}), {"qualification/second"})
                    # Two local copies and two completed fork runs are all two
                    # days old. Retain one public carrier for the shared fix.
                    fork["gh-pages"] = two
                    for branch in dropping:
                        run("update-ref", cleanup.HEADS + branch, fork[branch])
                    with contextlib.ExitStack() as controls:
                        for function, value in {
                            "check_origin": None, "remote_refs": fork, "public_bases": two,
                            "live_protections": (set(), set()), "tracker_protections": set(),
                            "api": ["master"], "completed_runs": {(n, fork[n]) for n in dropping},
                        }.items():
                            controls.enter_context(mock.patch.object(cleanup, function, return_value=value))
                        self.assertEqual({(c.scope, c.name, c.reason) for c in cleanup.plan(7)},
                                         {(scope, n, reason) for n in dropping - witness for scope, reason in (
                                             ("fork", cleanup.COMPLETED_CI), ("local", cleanup.LOCAL_CI))})
                    for branch in dropping:
                        run("update-ref", "-d", cleanup.HEADS + branch, fork[branch])
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
                    self.assertEqual(cleanup.published_reflogs({name: two}, {two}), set())
                    self.assertEqual(cleanup.published_reflogs({name: two}, {two, descendant}), {name})
                    self.assertEqual(cleanup.published_reflogs({name: one}, {two, descendant}), set())
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
                    logs = {name: (src / "logs" / full).read_text()}
                    run("update-ref", full, one, two)
                    run("update-ref", full, two, one)
                    future = dt.datetime.now(dt.timezone.utc) + dt.timedelta(days=1)
                    with self.assertRaisesRegex(ValueError, "changed since archival"):
                        cleanup.delete_local([row], logs, future, future)
                    self.assertEqual(run("rev-parse", full).decode().strip(), two)
                    with self.assertRaises(subprocess.CalledProcessError):
                        run("push", "--atomic", f"--force-with-lease={full}:{one}", "origin", ":" + full)
                    self.assertEqual(cleanup.remote_refs("origin"), {name: two})
                    run("push", "--atomic", f"--force-with-lease={full}:{two}", "origin", ":" + full)
                    cleanup.delete_local([row], {name: (src / "logs" / full).read_text()}, future, future)
                    self.assertEqual(cleanup.remote_refs("origin"), {})
                    self.assertEqual(run("for-each-ref", "--format=%(refname)", "refs/branch-cleanup").decode().strip(),
                                     "refs/branch-cleanup/archive")
                    run("update-ref", full, two, "0" * 40)
                    self.assertEqual(run("rev-parse", full).decode().strip(), two)
                    self.assertEqual(cleanup.local_activity({name: two}, dt.datetime.now(dt.timezone.utc)
                                                           - dt.timedelta(days=7)), {name})


if __name__ == "__main__":
    unittest.main()

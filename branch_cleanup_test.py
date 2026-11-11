import datetime as dt
import contextlib
import json
import os
from pathlib import Path
import subprocess
import tempfile
import unittest
from unittest import mock

import branch_cleanup as cleanup


class BranchCleanupTest(unittest.TestCase):
    def test_native_merged_source_requires_exact_reachable_and_idle_fork_ref(self):
        name, tip, moved = "topic", "a" * 40, "b" * 40
        row = {"ref": name, "head": tip, "number": 82, "status": "merged", "headRepo": cleanup.FORK}
        pr = {"number": 82, "state": "MERGED", "headRefName": name, "headRefOid": tip,
              "baseRepository": {"nameWithOwner": cleanup.UPSTREAM}, "headRepository": {"nameWithOwner": cleanup.FORK}}
        connection = {"pageInfo": {"hasNextPage": False}, "nodes": [pr]}
        ref = {"target": {"oid": tip}, "associatedPullRequests": connection}

        def native(*args):
            self.assertIn("refs/heads/topic", args[-1])
            return json.dumps({"data": {"repository": {"b0": ref}}}).encode()

        with mock.patch.object(cleanup, "git", return_value=tip.encode()), mock.patch.object(cleanup, "command", side_effect=native) as api:
            self.assertEqual(cleanup.live_merged_sources([row], {name: tip}, {}, tip), {name: 82})
            for fork, local in [({name: moved}, {}), ({name: tip}, {name: moved}), ({}, {})]:
                api.reset_mock()
                self.assertFalse(cleanup.live_merged_sources([row], fork, local, tip))
                api.assert_not_called()
            ref["target"]["oid"] = moved
            self.assertFalse(cleanup.live_merged_sources([row], {name: tip}, {}, tip))
            ref["target"]["oid"] = tip
            pr["headRefOid"] = moved
            self.assertFalse(cleanup.live_merged_sources([row], {name: tip}, {}, tip))
            pr["headRefOid"] = tip
            connection["nodes"].append({**pr, "state": "OPEN", "baseRepository": {"nameWithOwner": "other/gvisor"}})
            self.assertFalse(cleanup.live_merged_sources([row], {name: tip}, {}, tip))
            connection["nodes"].pop()
            connection["pageInfo"]["hasNextPage"] = True
            self.assertFalse(cleanup.live_merged_sources([row], {name: tip}, {}, tip))
        with mock.patch.object(cleanup, "git", return_value=moved.encode()), mock.patch.object(cleanup, "command") as api:
            self.assertFalse(cleanup.live_merged_sources([row], {name: tip}, {}, moved))
            api.assert_not_called()

    def test_merged_card_identity_survives_retiring_its_ref(self):
        name, previous = "review/archive", "a" * 40
        merged = {name: 82, "left": 83, "right": 84}
        branch = {"id": "branch:" + name, "type": "branch", "repo": cleanup.FORK, "ref": name,
                  "inactive": True, "head": previous, "url": f"https://github.com/{cleanup.FORK}/commit/{previous}"}
        source = {"id": "pr:82", "type": "pr", "ref": name, "number": 82}
        registry = {"nodes": [branch, source], "edges": [{"from": source["id"], "to": branch["id"]}]}
        snapshot = {"prs": [], "oldReview": "https://github.com/google/gvisor/compare/tamird:left...tamird:right"}
        names, tips = set(), set()
        cleanup.tracker_metadata(registry, snapshot, merged, names, tips)
        self.assertEqual(names, {"left", "right"})
        self.assertIn(previous, tips)
        unconfirmed = {"status": "merged", "headRepo": cleanup.FORK, "ref": "moved"}
        cleanup.tracker_metadata(registry, {"prs": [unconfirmed]}, merged, names, set())
        self.assertIn("moved", names)
        for command in [f"git log main...{name}", f'git -C "repo" diff {name}..main']:
            names.clear()
            cleanup.tracker_metadata(registry, {**snapshot, "archivedSummary": "Run `" + command + "`."}, merged, names, set())
            self.assertIn(name, names)
        for action in [{"branch": name}, {"command": "git cherry-pick origin/" + name}]:
            with self.subTest(action=action):
                names.clear()
                cleanup.tracker_metadata({**registry, "investigations": [action]}, snapshot, merged, names, set())
                self.assertIn(name, names)
        discussion = {"status": "open", "feedback": {"items": [{"body": "git switch " + name}]}}
        for key in ["prs", "reviewPRs"]:
            names.clear()
            cleanup.tracker_metadata(registry, {**snapshot, key: [discussion]}, merged, names, set())
            self.assertIn(name, names)
        names.clear()
        issue = {"githubState": "open", "comments": [{"body": "git switch " + name}]}
        cleanup.tracker_metadata(registry, {**snapshot, "issues": {"issue:1": issue}}, merged, names, set())
        self.assertIn(name, names)
        branch["additionalReviews"] = [{"status": "pending"}]
        names.clear()
        cleanup.tracker_metadata(registry, snapshot, merged, names, set())
        self.assertIn(name, names)
        del branch["additionalReviews"]
        branch["followup"] = {"nextAction": "Run git log origin/" + name}
        names.clear()
        cleanup.tracker_metadata(registry, snapshot, merged, names, set())
        self.assertIn(name, names)

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
            "https://github.com/google/gvisor/compare/master...tamird:gvisor:other/tip "
            "https://raw.githubusercontent.com/tamird/gvisor/refs/heads/raw-name/BUILD "
            "https://github.com/tamird/gvisor/archive/refs/heads/archived/name.tar.gz"
        ), {"qualification", "qualification/a", "qualification/a/file.go", "base-name", "tip/with-slash", "other/tip",
            "raw-name", "raw-name/BUILD", "archived/name"})

    def test_payload_rejects_mixed_product_and_validation_changes(self):
        with mock.patch.object(cleanup, "git", return_value=(
                b"\x1eaaa\n\n.github/workflows/build.yml\ntest/rbe/report.py\n"
                b"\x1ebbb\n\n.github/workflows/build.yml\npkg/new-product.go\n")):
            self.assertEqual(cleanup.payload_paths({"aaa", "bbb"}), {
                "aaa": [".github/workflows/build.yml", "test/rbe/report.py"]})

    def test_published_descendant_must_survive_planning_and_deletion(self):
        with tempfile.TemporaryDirectory(prefix="gvisor-cleanup-fixture-") as tmp:
            repo = Path(tmp) / "local.git"
            subprocess.run(["git", "init", "--bare", "--quiet", str(repo)], check=True)
            old = (dt.datetime.now(dt.timezone.utc) - dt.timedelta(days=10)).isoformat()
            with mock.patch.dict(os.environ, {"GIT_DIR": str(repo), "GIT_ALLOW_PROTOCOL": "file",
                                              "GIT_CONFIG_NOSYSTEM": "1", "GIT_CONFIG_GLOBAL": os.devnull,
                                              "GIT_AUTHOR_DATE": old, "GIT_COMMITTER_DATE": old}):
                run = cleanup.git
                run("config", "user.name", "Fixture")
                run("config", "user.email", "fixture@example.com")
                run("config", "core.logAllRefUpdates", "true")
                tree = run("mktree").decode().strip()

                def commit(message, *parents, contents=tree):
                    args = [arg for parent in parents for arg in ("-p", parent)]
                    return run("commit-tree", contents, *args, "-m", message).decode().strip()

                base = commit("master")
                tip = commit("qualified source", base)
                named_tip = commit("still named in the tracker", base)
                retained = commit("retained proposal", tip, named_tip)
                blob = run("hash-object", "-w", "--stdin", data=b"change").decode().strip()
                src = run("mktree", data=cleanup.lines([f"100644 blob {blob}\tproduct.go"])).decode().strip()
                private = commit("unpublished previous revision", base, contents=src)
                wf = run("mktree", data=cleanup.lines([f"100644 blob {blob}\tjob.yml"])).decode().strip()
                github = run("mktree", data=cleanup.lines([f"040000 tree {wf}\tworkflows"])).decode().strip()
                ci = run("mktree", data=cleanup.lines([f"040000 tree {github}\t.github"])).decode().strip()
                private_ci = commit("completed qualification on a since-deleted fork ref", base, contents=ci)
                local_ci, rewound = "qualification/old-ci", "review/reset-to-master"
                run("update-ref", cleanup.HEADS + local_ci, private_ci)
                run("update-ref", cleanup.HEADS + rewound, private)
                run("update-ref", cleanup.HEADS + rewound, base, private)
                eligible, reset = "qualification/published", "qualification/amended"
                named, witness = "diagnostics/named-in-card", "proposal/active"
                fork = {"master": base, "gh-pages": base, witness: retained, eligible: tip, reset: tip, named: named_tip}
                run("update-ref", cleanup.HEADS + eligible, tip)
                run("update-ref", cleanup.HEADS + "research/local-only", tip)
                run("update-ref", cleanup.HEADS + reset, private)
                run("update-ref", cleanup.HEADS + reset, tip, private)
                run("update-ref", cleanup.HEADS + "local-only-descendant", retained)

                def protected(*_):
                    return {named, witness}, set()

                with contextlib.ExitStack() as controls:
                    for function, value in {"check_origin": None, "public_bases": base,
                                            "tracker_protections": set(), "api": ["master"],
                                            "completed_runs": {(local_ci, private_ci)}}.items():
                        controls.enter_context(mock.patch.object(cleanup, function, return_value=value))
                    controls.enter_context(mock.patch.object(cleanup, "live_protections", side_effect=protected))
                    observed = controls.enter_context(mock.patch.object(cleanup, "remote_refs", return_value=fork))
                    rows = cleanup.plan(7)
                    self.assertEqual({(r.scope, r.name, r.reason) for r in rows},
                                     {(scope, name, cleanup.PUBLISHED_BRANCH)
                                      for scope, name in [("fork", eligible), ("local", eligible), ("fork", reset), ("local", "research/local-only")]})
                    for name, oid, reason in [(local_ci, private_ci, cleanup.COMPLETED_CI),
                                              (rewound, base, "reachable from a published base")]:
                        with self.subTest(name=name), mock.patch.object(cleanup, "archive", side_effect=AssertionError):
                            with self.assertRaisesRegex(ValueError, "unpublished local history"):
                                cleanup.apply([cleanup.Candidate("local", name, oid, reason)], 7)
                    removed = {k: v for k, v in fork.items() if k != witness}
                    self.assertNotIn(tip, cleanup.retained_public_history(fork, {witness}, {witness}))
                    self.assertIn(tip, cleanup.retained_public_history({**removed, "stable": retained}, set(), set()))
                    observed.return_value = removed
                    self.assertFalse(any(r.reason == cleanup.PUBLISHED_BRANCH for r in cleanup.plan(7)))
                    fork_row = [r for r in rows if r.scope == "fork" and r.name == eligible]
                    with mock.patch.object(cleanup, "archive", side_effect=AssertionError):
                        with self.assertRaisesRegex(ValueError, "lost its retained public descendant"):
                            cleanup.apply(fork_row, 7)
                    # The witness still existed at archival, then moved before the batch.
                    observed.side_effect = [fork, {**fork, witness: base}]
                    with self.assertRaisesRegex(ValueError, "lost its retained public descendant"):
                        cleanup.apply(fork_row, 7)
                    self.assertEqual(run("rev-parse", cleanup.HEADS + eligible).decode().strip(), tip)

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
                    run("config", "branch.personal.remote", "private")
                    self.assertEqual(cleanup.local_activity({}, dt.datetime.now(dt.timezone.utc)), set())
                    self.assertEqual(cleanup.local_activity({"personal": "absent"}, dt.datetime.now(dt.timezone.utc)), {"personal"})
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
                    run("update-ref", "refs/heads/merged", one)
                    run("update-ref", "refs/heads/merged", two, one)
                    merged = [cleanup.Candidate("local", "merged", two, cleanup.MERGED_SOURCE)]
                    self.assertEqual(cleanup.unpublished_locals(merged, {two}), {"merged"})
                    self.assertEqual(cleanup.unpublished_locals(merged, {descendant}), set())
                    run("update-ref", "-d", "refs/heads/merged", two)
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
                        with mock.patch.object(cleanup, "completed_runs", side_effect=AssertionError):
                            self.assertEqual(cleanup.plan(7, merged_only=True), [])
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

"""Collect the original Linux test records for overlay waiter lifetime."""
import hashlib
import json
import os
from pathlib import Path
import re
import subprocess

import timer_teardown as donor

out = Path(os.environ["RUNNER_TEMP"]) / "waiter-results"
out.mkdir(exist_ok=True)
product = "a748da63e1f6a63db80db838d67c0763bd545468"
parent = "70f8576af3ddb78d5bfc9be51b14460a2253a2e4"
previous = "488b298b86176fbb4dd442b9b3c54d4ecfbcc935"
probe = "0a282742f8d8352fb40a670637a0d396c29aa7b7"
fs = "//pkg/sentry/fsimpl/overlay:overlay_test"
nogo = "//pkg/sentry/fsimpl/overlay:overlay_nogo"
linux = "//pkg/sentry/syscalls/linux:linux_test"
epoll = "//test/syscalls:epoll_test_runsc_systrap_directfs"
code_file = "pkg/sentry/fsimpl/overlay/regular_file.go"
unit = "TestUnregisterAfterCopyUp"
lifetime = "TestOverlayLowerPollLifetime"
nested = "TestStackedPollBinding"
close = "TestPollWaiterAfterOverlayCopyUp"
report = {"source": product, "fixture": probe, "parent": parent, "previous": previous, "arms": {}, "verified": False}

def git(*args):
    return subprocess.check_output(["git", *args], text=True).strip()

def save():
    (out / "result.json").write_text(json.dumps(report, indent=2) + "\n")

def verify(name, source, cases, *, expect_failure=False, args=(), copy_product=None):
    git("checkout", "-q", "--detach", source)
    if copy_product:
        git("restore", "--source", copy_product, "--", code_file)
    assert git("rev-parse", "HEAD") == source
    changes = git("diff", "--name-only")
    assert changes == (code_file if copy_product else ""), changes
    result = report["arms"][name] = {"tree": source, "implementation": copy_product or source, "records": {}}
    save()
    bep = out.parent / (name + ".bep")
    try:
        for attempt in range(12):
            bep.unlink(missing_ok=True)
            cmd = [str(out.parent / "bazel"), "--bazelrc=" + os.environ["RBE_RC"], "test",
                   "--config=x86_64", "-c", "opt", "--jobs=48", "--strategy=TestRunner=remote",
                   "--noremote_local_fallback", "--keep_going", "--nocache_test_results",
                   "--flaky_test_attempts=1", "--runs_per_test=1", "--test_sharding_strategy=disabled",
                   "--remote_download_outputs=toplevel", "--remote_download_regex=.*[/]test[.](xml|log)$",
                   "--test_timeout=480", "--test_output=errors", "--test_env=GO_TEST_WRAP_TESTV=1",
                   "--build_event_json_file=" + str(bep), "--lockfile_mode=off",
                   *["--distdir=" + str(d) for d in sorted(donor.dirs)], *args, *cases]
            with (out.parent / (name + ".console")).open("w+") as f:
                proc = subprocess.run(cmd, stdout=f, stderr=subprocess.STDOUT)
                f.seek(0)
                console = f.read()
            events = [json.loads(s) for s in bep.read_text().splitlines()] if bep.exists() else []
            rows = [e for e in events if "testResult" in e]
            if rows or not donor.acquire(console):
                break
        result["exit"] = proc.returncode
        result["invocation"] = next((e["started"]["uuid"] for e in events if "started" in e), None)
        for e in rows:
            t = e["testResult"]
            identity = e["id"]["testResult"]
            label = identity["label"]
            assert label in cases and label not in result["records"]
            r = result["records"][label] = {"outcome": t["status"], "job": t.get("executionInfo", {}).get("strategy"), "outputs": {}}
            for o in t["testActionOutput"]:
                if o["name"] not in ("test.log", "test.xml"):
                    continue
                path = Path("bazel-testlogs") / label[2:].replace(":", "/") / o["name"]
                data = path.read_bytes()
                assert not o.get("digest") or o["digest"] == hashlib.sha256(data).hexdigest()
                assert not o.get("length") or int(o["length"]) == len(data)
                filename = name + "-" + label.split(":")[-1] + "." + o["name"].split(".")[-1]
                (out / filename).write_bytes(data)
                r["outputs"][o["name"]] = filename
            log = (out / r["outputs"]["test.log"]).read_text(errors="replace")
            assert t["status"] == ("FAILED" if expect_failure else "PASSED"), (label, log[-450:])
            assert r["job"] == "remote" and not t.get("cachedLocally") and not t.get("executionInfo", {}).get("cachedRemotely")
            absent = [pattern for pattern in cases[label] if not re.search(pattern, log, re.M)]
            assert not absent, (label, absent)
        assert set(result["records"]) == set(cases), [l[:300] for l in console.splitlines() if l.startswith("ERROR:")][-8:]
        assert proc.returncode == (3 if expect_failure else 0)
        result["verified"] = True
    except Exception as exc:
        result["error"] = str(exc)[:1300]
    finally:
        if copy_product:
            git("restore", "--", code_file)
        save()
        print(name, result, flush=True)

manifest = json.loads((out.parent / "inputs.json").read_text())
donor.known.update((r[2], r) for r in manifest)
donor.assets.DIST.mkdir(parents=True, exist_ok=True)
# The same permanent regression runs on the poll-only implementation and the exact proposal.
verify("stock", product, {fs: ["removed overlay waiter received a hostname notification"]},
       expect_failure=True, copy_product=parent, args=["--test_arg=-test.v", "--test_filter=^"+unit+"$"])
verify("product", product, {fs: ["^--- PASS: "+unit], nogo: [r"^amd64\.\.\.\s+PASS",r"^arm64\.\.\.\s+PASS"]})
# Keep fixture and target source fixed while comparing former and current drop timing.
verify("previous", probe, {linux: [r"old lower refs before close: got [0-9]+, want [0-9]+"]},
       expect_failure=True, copy_product=previous, args=["--test_arg=-test.v", "--test_filter=^"+lifetime+"$"])
verify("race", probe, {linux: ["^--- PASS: "+c for c in (close,lifetime,nested)] +
        ["innerFirst="+v+" selected final proc file" for v in ("true","false")]},
       args=["--config=race", "--test_arg=-test.v", "--test_filter=^("+close+"|"+lifetime+"|"+nested+")$"])
verify("guest", probe, {epoll:["OVERLAY_ASYNC_FINISHED: checkpoint=0;"],
                         epoll+"_save":["OVERLAY_ASYNC_FINISHED: checkpoint=1;"]})
report["verified"] = all(v.get("verified") for v in report["arms"].values())
save()
raise SystemExit(not report["verified"])

"""Collect the original Linux test records for overlay waiter lifetime."""
import hashlib
import json
import os
from pathlib import Path
import re
import subprocess
import time
from xml.etree import ElementTree

import timer_teardown as donor

out = Path(os.environ["RUNNER_TEMP"]) / "waiter-results"
out.mkdir(exist_ok=True)
product = "d01969e4cf73163e16243bd0d8edb666fce733db"
parent = "aaab1ecb082e650ee857a0f8366229a1f37e3143"
previous_control_run = 42729241382
probe = "f7b21ce26f870181dbae04f4d34ac051f9fea6aa"
fs = "//pkg/sentry/fsimpl/overlay:overlay_test"
nogo = "//pkg/sentry/fsimpl/overlay:overlay_nogo"
linux = "//pkg/sentry/syscalls/linux:linux_test"
epoll = "//test/syscalls:epoll_test_runsc_systrap_directfs"
code_file = "pkg/sentry/fsimpl/overlay/regular_file.go"
unit = "TestUnregisterAfterCopyUp"
lifetime = "TestOverlayLowerPollLifetime"
nested = "TestStackedPollBinding"
close = "TestPollWaiterAfterOverlayCopyUp"
report = {"source": product, "fixture": probe, "parent": parent, "historic_control": previous_control_run, "arms": {}, "verified": False}

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
    deadline = time.monotonic() + (38 * 60 if name == "guest" else 24 * 60)
    print("Started", name, source, flush=True)
    bep = out.parent / (name + ".bep")
    try:
        for attempt in range(12):
            bep.unlink(missing_ok=True)
            cmd = [str(out.parent / "bazel"), "--nosystem_rc", "--nohome_rc", "--host_jvm_args=-Xmx3g", "test",
                   "--config=x86_64", "-c", "opt", "--jobs=4", "--strategy=TestRunner=local",
                   "--remote_executor=", "--remote_cache=", "--bes_backend=", "--spawn_strategy=processwrapper-sandbox,local",
                   "--keep_going", "--nocache_test_results", "--flaky_test_attempts=1", "--runs_per_test=1",
                   "--test_sharding_strategy=disabled",
                   "--test_timeout=480", "--test_output=errors", "--test_env=GO_TEST_WRAP_TESTV=1",
                   "--build_event_json_file=" + str(bep), "--lockfile_mode=off",
                   *["--distdir=" + str(d) for d in sorted(donor.dirs)], *args, *cases]
            with (out.parent / (name + ".console")).open("w+") as f:
                proc = subprocess.run(cmd, stdout=f, stderr=subprocess.STDOUT, timeout=max(1,deadline-time.monotonic()))
                f.seek(0)
                console = f.read()
            lines = console.splitlines()
            locations = {n for i, line in enumerate(lines) if line.startswith(("ERROR:", "FATAL:")) for n in range(i, min(i + 4, len(lines)))}
            with (out / (name + ".diagnostics")).open("w") as d:
                for n in sorted(locations)[-28:]:
                    d.write(lines[n][:700] + "\n")
                d.write("\n".join(line[:700] for line in lines[-14:])+"\n")
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
            assert r["job"] in ("local", "standalone") and not t.get("cachedLocally") and not t.get("executionInfo", {}).get("cachedRemotely")
            checks = cases[label]
            if label == fs and not expect_failure:
                xml = ElementTree.parse(out / r["outputs"]["test.xml"])
                case, = xml.findall(".//testcase")
                assert case.attrib.get("name") == unit and all(case.find(k) is None for k in ("skipped","error","failure"))
                checks = []
            absent = [pattern for pattern in checks if not re.search(pattern, log, re.M)]
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
verify("race", probe, {linux: ["^--- PASS: "+c for c in (close,lifetime,nested)] +
        ["innerFirst="+v+" selected final proc file" for v in ("true","false")]},
       args=["--config=race", "--test_arg=-test.v", "--test_filter=^("+close+"|"+lifetime+"|"+nested+")$"])
verify("lockdep", probe, {linux: ["^--- PASS: "+nested, "innerFirst=true selected final proc file", "innerFirst=false selected final proc file"],
                           "//pkg/sync/locking:locking_test": ["^--- PASS: TestReverseNestedDeeper", "Got expected panic"]},
       args=["--config=lockdep", "--test_arg=-test.v", "--test_filter=^("+nested+"|TestReverseNestedDeeper)$"])
verify("guest", probe, {epoll:["OVERLAY_ASYNC_FINISHED: checkpoint=0;"],
                         epoll+"_save":["OVERLAY_ASYNC_FINISHED: checkpoint=1;"]})
report["verified"] = all(v.get("verified") for v in report["arms"].values())
save()
raise SystemExit(not report["verified"])

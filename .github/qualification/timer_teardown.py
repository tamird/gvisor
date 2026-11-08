"""Check the two generated kernel architectures and both real timer syscall owners."""
import hashlib
import json
import os
from pathlib import Path
import re
import subprocess
import time
from urllib.parse import unquote, urlsplit
import xml.etree.ElementTree as ET

import public_assets as assets
import tldk_public_inputs as inputs

WORK = Path(os.environ["RUNNER_TEMP"]) / "posix-teardown"
NG = "//pkg/sentry/kernel:kernel_nogo"
DUT = ["//test/syscalls:timers_test_native", "//test/syscalls:timers_test_runsc_systrap"]
CASE = "IntervalTimerTest.ExecAndExitWithPendingTimer"
GROUP = Path("pkg/sentry/kernel/thread_group.go")
KERNEL = Path("pkg/sentry/kernel/kernel.go")
SOURCE = "dae796fad1c847fcaa5a8e77e6033363b1235695"
LIMIT = time.monotonic() + 105 * 60
dirs = {assets.DIST}
seen = {}
known = {}
report = {"source": SOURCE, "kernel": os.uname().release, "cpu": os.uname().machine,
          "verified": False, "arms": {}, "inputs": seen}


def save():
    temp = WORK / "result.tmp"
    temp.write_text(json.dumps(report, indent=2, sort_keys=True) + "\n")
    temp.replace(WORK / "result.json")


def acquire(text):
    requests = set(re.findall(r"Error downloading \[(https://github[.]com/[\w./+-]+)\] to (\S+)", text))
    direct = dict(re.findall(r'^bazel_dep\(name = "([\w.-]+)", version = "([\w.+-]+)"',
                             Path("MODULE.bazel").read_text(), re.M))
    added = False
    for url, dest in sorted(requests):
        if url in seen or len(seen) >= 64:
            continue
        budget = inputs.LIMIT - sum(v.get("bytes", 0) for v in seen.values())
        try:
            if url in known:
                directory, row = inputs.materialize(*known[url], budget)
            elif (arc := inputs.ARCHIVE.fullmatch(url)) and (module := assets.DIR.search(dest)):
                tag = arc[3].split("/")[-1].removeprefix("v")
                directory, row = inputs.materialize(module[1], direct.get(module[1], tag), url,
                                                     None, None, None, budget)
            else:
                directory, row = assets.release_distfile(url, assets.DIST, remaining_bytes=budget)
            dirs.add(directory)
            seen[url] = row
            added = True
        except (ValueError, AssertionError, KeyError, OSError) as exc:
            seen[url] = {"unavailable": str(exc)[:160]}
    return added


def original(name, label, event):
    ident, value = event["id"]["testResult"], event["testResult"]
    assert ident["label"] == label
    row = {"configuration": ident["configuration"]["id"], "status": value["status"],
           "strategy": value.get("executionInfo", {}).get("strategy"),
           "cached": bool(value.get("cachedLocally") or value.get("executionInfo", {}).get("cachedRemotely"))}
    report["arms"][name]["tests"][label] = row
    outputs = row["outputs"] = {}
    for output in value["testActionOutput"]:
        kind = output["name"]
        if kind not in ("test.xml", "test.log"):
            continue
        uri = urlsplit(output["uri"])
        assert uri.scheme == "file" and not uri.netloc
        data = Path(unquote(uri.path)).read_bytes()
        digest = hashlib.sha256(data).hexdigest()
        assert 0 < len(data) <= 24<<20 and (not output.get("digest") or output["digest"] == digest)
        assert "length" not in output or len(data) == int(output["length"])
        file = WORK / (name + "-" + label.split(":")[-1] + "." + kind.split(".")[1])
        file.write_bytes(data)
        outputs[kind] = {"name": file.name, "sha256": digest, "bytes": len(data)}
    assert not row["cached"] and row["strategy"] in ("local", "standalone")
    text = (WORK / outputs["test.log"]["name"]).read_text(errors="replace")
    root = ET.parse(WORK / outputs["test.xml"]["name"])
    if label == NG:
        parts = re.split(r"(?m)^(amd64|arm64)\.\.\.", text)
        assert len(parts) == 5 and set(parts[1::2]) == {"amd64", "arm64"}
        for arch, body in zip(parts[1::2], parts[2::2]):
            lines = body.strip().splitlines()
            if name == "positive":
                assert lines == ["PASS"] and row["status"] == "PASSED"
            else:
                rows = [s for s in lines if s.startswith("checklocks: ")]
                assert row["status"] == "FAILED" and len(rows) == 3
                for field, line in report["negative_lines"].items():
                    file = "kernel.go" if field == "Release" else "thread_group.go"
                    hit, = [s for s in rows if f"pkg/sentry/kernel/{file}:{line}:" in s]
                    assert "timerMu" in hit and hit.endswith(f"(GOOARCH={arch}, GOOS=linux)")
                    if field == "Release":
                        assert "to call Release" in hit
                    else:
                        assert "invalid field access" in hit and re.search(r"(?:accessing|reading) "+field+r" \(", hit)
                row[arch] = rows
                continue
            row[arch] = "PASS"
    else:
        assert row["status"] == "PASSED"
        cases = list(root.iter("testcase"))
        assert len(cases) == 1 and (cases[0].get("classname"),cases[0].get("name")) == tuple(CASE.split("."))
        assert not any(cases[0].find(t) is not None for t in ("skipped","failure","error"))
        before, = re.findall(r"(?m)^TIMER_PENDING pid=(\d+) signal_id=(\d+) silent_id=(\d+)$", text)
        after, = re.findall(r"(?m)^TIMER_EXEC pid=(\d+) signal_id=(\d+) silent_id=(\d+) old_errors=EINVAL,EINVAL live_id=(\d+) exit_pending=1$", text)
        assert before == after[:3] and before[1] != before[2]
        assert "POTENTIAL DEADLOCK" not in text
        row.update(pending_pid=int(before[0]), removed_ids=list(map(int, before[1:])), live_at_exit=int(after[3]), case=CASE)
    row["verified"] = True


def run(name, labels, extra):
    arm = report["arms"][name] = {"tests": {}, "preparation": []}
    bep = WORK / (name + ".jsonl")
    for _ in range(42):
        bep.unlink(missing_ok=True)
        args = [str(WORK / "bazel"), "--nosystem_rc", "--nohome_rc", "--host_jvm_args=-Xmx4g", "test",
                "--config=x86_64", "--jobs=4", "--lockfile_mode=off", "--remote_cache=", "--remote_executor=",
                "--bes_backend=", "--spawn_strategy=processwrapper-sandbox,local",
                "--strategy=GoStdlib=processwrapper-sandbox", "--strategy=GoStdlibList=processwrapper-sandbox",
                "--modify_execution_info=^(GoStandardLibraryAnalysis|GoStaticAnalysis)$=-no-sandbox",
                "--strategy=TestRunner=local", "--test_timeout=300", "--nocache_test_results",
                "--runs_per_test=1", "--flaky_test_attempts=1", "--test_sharding_strategy=disabled",
                "--build_tag_filters=", "--test_tag_filters=", "--keep_going",
                "--build_event_json_file="+str(bep), "--build_event_json_file_path_conversion=false",
                *["--distdir="+str(d) for d in sorted(dirs)], *extra, *labels]
        remaining = LIMIT-time.monotonic()
        if remaining <= 0:
            raise TimeoutError("job execution budget")
        save()
        with (WORK / (name+".console")).open("w+") as console:
            result = subprocess.run(args, stdout=console, stderr=subprocess.STDOUT, timeout=remaining)
            console.seek(0)
            text = console.read()
        arm["exit"] = result.returncode
        events = [json.loads(s) for s in bep.open()] if bep.exists() else []
        arm["invocation"] = next((e["started"]["uuid"] for e in events if "started" in e), None)
        tests = [e for e in events if "testResult" in e]
        if tests:
            for event in tests:
                label = event["id"]["testResult"]["label"]
                assert label in labels and label not in arm["tests"]
                try:
                    original(name, label, event)
                except Exception as error:
                    arm["tests"].setdefault(label, {})["error"] = type(error).__name__ + " " + str(error)[:200]
            assert set(arm["tests"]) == set(labels) and all(v.get("verified") for v in arm["tests"].values())
            assert result.returncode == (3 if name == "negative" else 0)
            arm["verified"] = True
            return
        arm["preparation"].append({"exit": result.returncode, "errors": [s[:640] for s in text.splitlines()
                                  if s.startswith("ERROR:") or "Error downloading [" in s][:12]})
        save()
        if not acquire(text):
            raise RuntimeError("no TestResult: see original preparation diagnostics")
    raise RuntimeError("public archive attempt limit")


def main():
    assert os.uname().machine == "x86_64" and os.getuid() != 0
    assert subprocess.check_output(["git", "rev-parse", "HEAD^"]).decode().strip() == SOURCE
    rows = json.loads((WORK/"inputs.json").read_text())
    pgv = ["protoc-gen-validate","1.3.0","https://github.com/bufbuild/protoc-gen-validate/archive/refs/tags/v1.3.0.tar.gz",
           "26dc6db523ee943af51fdfc292658a2dbe7046735654f1dc839745988f0fe26b",190514,None]
    coral = [None,None,"https://github.com/google-coral/crosstool/archive/8e885509123395299bed6a5f9529fdc1b9751599.tar.gz",
             "f86d488ca353c5ee99187579fe408adb73e9f2bb1d69c6e3a42ffb904ce3ba01",17312,None]
    assert all(x in Path("tools/bazeldefs/extensions/coral_crosstool.bzl").read_text() for x in coral[2:4])
    known.update((r[2],r) for r in [*rows,pgv,coral])
    assets.DIST.mkdir(parents=True,exist_ok=True)
    for d, row in inputs.seed([r for r in rows if r[0] and r[0] != "llvm"]+[pgv,coral]):
        if d:
            dirs.add(d)
            seen[row["source"]] = row
        else:
            report.setdefault("unavailable_seeds",[]).append(row)
    baseline = {p:p.read_text() for p in (GROUP,KERNEL)}
    try:
        for name, labels, opts in [("positive", [NG], []), ("negative", [NG], []),
                                   ("runtime", DUT, ["--config=lockdep", "--test_env=GTEST_FILTER="+CASE])]:
            if name == "negative":
                fn = "func (tg *ThreadGroup) destroyIntervalTimers() {\n"
                early = "\tempty := len(tg.timers) == 0\n\tif tg.nextTimerID == 0 && empty { return }\n"
                release = "\t\ttg.Release(ctx)\n"
                assert baseline[GROUP].count(fn) == 1 and baseline[KERNEL].count(release) == 1
                GROUP.write_text(baseline[GROUP].replace(fn, fn+early))
                KERNEL.write_text(baseline[KERNEL].replace(release, "\t\ttg.timerMu.Lock()\n"+release+"\t\ttg.timerMu.Unlock()\n"))
                report["negative_lines"] = {field: next(i for i,s in enumerate(p.read_text().splitlines(),1) if s.strip() == needle)
                    for field,p,needle in [("timers",GROUP,"empty := len(tg.timers) == 0"),
                        ("nextTimerID",GROUP,"if tg.nextTimerID == 0 && empty { return }"),("Release",KERNEL,"tg.Release(ctx)")]}
            try:
                run(name,labels,opts)
            except Exception as e:
                report["arms"].setdefault(name,{})["error"] = type(e).__name__ + " " + str(e)[:250]
            finally:
                if name == "negative":
                    for path, text in baseline.items():
                        path.write_text(text)
                save()
    finally:
        for path, text in baseline.items():
            assert path.read_text() == text
    report["verified"] = all(report["arms"][name].get("verified") for name in ("positive","negative","runtime"))
    save()
    assert report["verified"]


if __name__ == "__main__":
    try:
        main()
    finally:
        save()

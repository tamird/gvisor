"""Check POSIX timer ownership during checkpoint, exec, and exit."""
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
DUT = "//test/syscalls:timers_test_runsc_systrap_directfs"
SAVE = DUT + "_save"
NATIVE = "//test/syscalls:timers_test_native"
NOGO = "//pkg/sentry/kernel:kernel_nogo"
KERNEL = Path("pkg/sentry/kernel/kernel.go")
CASE = "IntervalTimerTest.ExecAndExitWithPendingTimer"
SOURCE = "5b9d447ba1bbfd67e6db860d585239167d1bc9f9"
LIMIT = time.monotonic() + 105 * 60
dirs = {assets.DIST}
seen = {}
known = {}
report = {"source": SOURCE, "kernel": os.uname().release, "cpu": os.uname().machine,
          "verified": False, "arms": {}, "inputs": seen,
          "previous_artifacts": [13617806820, 13618831468]}


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
    if label == NOGO:
        parts = re.split(r"(?m)^(amd64|arm64)\.\.\.", text)
        assert len(parts) == 5 and set(parts[1::2]) == {"amd64", "arm64"}
        assert row["status"] == ("FAILED" if name == "negative" else "PASSED")
        for arch, section in zip(parts[1::2], parts[2::2]):
            lines = section.strip().splitlines()
            if name == "negative":
                hit, = [line for line in lines if line.startswith("checklocks: ")]
                assert f"pkg/sentry/kernel/kernel.go:{report['unguarded_line']}:" in hit
                assert "invalid field access" in hit and "timerMu" in hit
                assert re.search(r"(?:accessing|reading) timers \(", hit)
                assert hit.endswith(f"(GOOARCH={arch}, GOOS=linux)")
                row[arch] = hit
            else:
                assert lines == ["PASS"]
                row[arch] = "PASS"
        row["verified"] = True
        return
    assert row["status"] == "PASSED"
    cases = list(root.iter("testcase"))
    assert len(cases) == 1 and (cases[0].get("classname"),cases[0].get("name")) == tuple(CASE.split("."))
    assert not any(cases[0].find(t) is not None for t in ("skipped","failure","error"))
    before, = re.findall(r"(?m)^TIMER_PENDING pid=(\d+) signal_id=(\d+) silent_id=(\d+)$", text)
    after, = re.findall(r"(?m)^TIMER_EXEC pid=(\d+) signal_id=(\d+) silent_id=(\d+) old_errors=EINVAL,EINVAL live_id=(\d+) exit_pending=1$", text)
    assert before == after[:3] and before[1] != before[2]
    resumed = re.findall(r"(?m)^TIMER_RESUMED pid=(\d+) signal_id=(\d+) silent_id=(\d+)$", text)
    if label == SAVE:
        assert resumed == [before]
        interval = text.split("TIMER_PENDING pid=", 1)[1].split("TIMER_RESUMED pid=", 1)[0]
        restores = re.findall(r"(?m)^.*Executing: \[[^\n]+ restore [^\n]*--image-path ([^\s]+) --bundle [^\n]+\]$", interval)
        assert len(restores) == 1, f"expected one nonempty checkpoint restore, found {len(restores)}"
        assert text.index("TIMER_RESUMED pid=") < text.index("TIMER_EXEC pid=")
        row["restored_checkpoint"] = Path(restores[0]).name
        row["restored_ids"] = list(map(int, before[1:]))
    else:
        assert not resumed
    assert "POTENTIAL DEADLOCK" not in text
    row.update(pending_pid=int(before[0]), removed_ids=list(map(int, before[1:])), live_at_exit=int(after[3]), case=CASE)
    row["verified"] = True


def capture_failure(arm, events, text, exit_code):
    observation = {"exit": exit_code, "actions": [], "targets": [], "aborted": []}
    for event in events:
        ident = event["id"]
        if "finished" in event:
            finished = event["finished"]
            observation["finished"] = {k: str(finished[k])[:1100] for k in ("exitCode","failureDetail") if k in finished}
        if "completed" in event and ident.get("targetCompleted",{}).get("label") in (DUT, SAVE, NATIVE, NOGO):
            result = event["completed"]
            observation["targets"].append({"id": ident, "success": bool(result.get("success")),
                                          "detail": str(result.get("failureDetail",""))[:1000]})
        if "aborted" in event and len(observation["aborted"]) < 20:
            observation["aborted"].append([str(ident)[:500],str(event["aborted"])[:1000]])
        action = event.get("action")
        if action and not action.get("success") and len(observation["actions"]) < 12:
            record = {"id": str(ident)[:800], "type": action.get("type"), "exit": action.get("exitCode"),
                      "detail": str(action.get("failureDetail",""))[:1000]}
            for name in ("stdout","stderr"):
                output = action.get(name,{})
                record[name] = {k: str(output[k])[:500] for k in ("name","digest","length") if k in output}
            observation["actions"].append(record)
    lines = text.splitlines()
    observation["errors"] = [s[:700] for s in lines if s.startswith(("ERROR:", "FAIL:")) or "Error downloading [" in s][:25]
    if exit_code or not any("testResult" in event for event in events):
        observation["tail"] = [s[:700] for s in lines[-28:]]
        for i,line in enumerate(lines):
            if line.startswith("ERROR:"):
                observation["first_error_context"] = [s[:700] for s in lines[i:i+18]]
                break
    arm["preparation"].append(observation)
    save()


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
            try:
                rc = subprocess.run(args, stdout=console, stderr=subprocess.STDOUT, timeout=remaining).returncode
            except subprocess.TimeoutExpired:
                arm["timed_out"] = True
                rc = 124
            console.seek(0)
            text = console.read()
        arm["exit"] = rc
        events = []
        if bep.exists():
            with bep.open() as stream:
                for line in stream:
                    if line.endswith("\n"):
                        events.append(json.loads(line))
        arm["invocation"] = next((e["started"]["uuid"] for e in events if "started" in e), None)
        capture_failure(arm, events, text, rc)
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
            assert rc == (3 if name == "negative" else 0)
            arm["verified"] = True
            return
        if arm.get("timed_out"):
            raise TimeoutError("systrap Bazel command exceeded the remaining budget")
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
    original_source = KERNEL.read_text()
    guard = "\t\t\tt.tg.timerMu.Lock()\n\t\t\tfor _, it := range t.tg.timers {\n\t\t\t\tit.PauseTimer()\n\t\t\t}\n\t\t\tt.tg.timerMu.Unlock()"
    assert original_source.count(guard) == 1
    mutant = original_source.replace(guard, "\n".join(guard.splitlines()[1:-1]))
    report["unguarded_line"] = mutant[:mutant.index("\t\t\t\tit.PauseTimer()")].count("\n")
    report["unguarded_sha256"] = hashlib.sha256(mutant.encode()).hexdigest()
    modes = [("positive", [NOGO], []), ("runtime", [DUT, NATIVE, SAVE],
             ["--config=lockdep", "--test_env=GTEST_FILTER="+CASE]), ("negative", [NOGO], [])]
    for name, labels, options in modes:
        try:
            if name == "negative":
                KERNEL.write_text(mutant)
            run(name, labels, options)
        except Exception as exc:
            report["arms"].setdefault(name, {})["error"] = type(exc).__name__ + " " + str(exc)[:300]
        finally:
            if name == "negative":
                KERNEL.write_text(original_source)
            save()
    assert KERNEL.read_text() == original_source
    report["verified"] = all(report["arms"][name].get("verified") for name, _, _ in modes)
    save()
    assert report["verified"]


if __name__ == "__main__":
    try:
        main()
    finally:
        save()
